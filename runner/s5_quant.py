#!/usr/bin/env python3
"""S5 QUANT: fifth strategy, PAPER ONLY (no wallet, no keys, no transactions). Own process, own 1 SOL paper account,
own log / CSV / SQLite. Never touches pump_sentinel.py or m4_momentum.py.

Built with the quant-desk method: scouted public pump.fun strategies, backtested them on real candles with real costs, and
runs the least-bad one (none passed the bar). Strategy = Outrider "Revival" lane, rules in s5_signals.make_revival and
exits from its card (s5_quant.toml [exit]). The SAME rule code is used by the backtest (/workspace/s5_research).
Live data: pump.fun list API (recently traded coins), Jupiter 1-minute chart candles, DexScreener prices, Jupiter quotes.
Only the fills are simulated (real Jupiter quotes + latency slippage + network fees + token-account rent).

  python3 s5_quant.py            # runs until SIGTERM/SIGINT; a restart resumes the same paper account
"""
import argparse, csv, json, logging, os, signal, sqlite3, sys, threading, time, tomllib, collections
from bot import sources, http, pumpfun
import s5_core as C
import s5_signals as S

ap = argparse.ArgumentParser()
ap.add_argument("--config", default="s5_quant.toml")
ap.add_argument("--new-run", action="store_true")
args = ap.parse_args()
CFG = tomllib.load(open(args.config, "rb"))
G, EN, EX, RK = CFG["general"], CFG["entry"], CFG["exit"], CFG["risk"]
assert G["paper"] is True, "S5 is paper only"
os.makedirs("data", exist_ok=True)
ACCT = "S5"
STATE_F, ACCT_F, RUN_F = "data/s5_state.json", "data/s5_account.json", "data/s5_run.txt"
if args.new_run or not os.path.exists(RUN_F):
    open(RUN_F, "w").write(time.strftime("S%Y%m%d-%H%M%S"))
    if os.path.exists(ACCT_F): os.replace(ACCT_F, ACCT_F + ".prev")
RUN = open(RUN_F).read().strip()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-8s %(message)s", datefmt="%H:%M:%S",
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(f"data/s5_quant_{RUN}.log")])
logging.getLogger("http").setLevel(logging.WARNING)
log = logging.getLogger("s5")
LOCK = threading.RLock()
STOP = threading.Event()
WSOL = sources.WSOL
LAT = G["latency_slippage_bps"] / 1e4
FEE = G["fee_base_sol"] + G["fee_priority_sol"] + G["fee_jito_sol"]
RENT = G["ata_rent_sol"]
CARD = dict(stop_pct=EX["stop_loss_pct"], tp=[tuple(x) for x in EX["take_profits"]], trail_pct=EX["trail_pct"], trail_arm_x=None,
            timeout_min=EX["max_hold_minutes"], close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=None)
SIG = S.make_revival(EN["min_age_minutes"], EN["quiet_window_minutes"], EN["quiet_max_volume_sol"], EN["wake_min_sol"], EN["max_price_vs_peak"])
JUP_CHART = "https://datapi.jup.ag/v2/charts/{mint}?interval=1_MINUTE&to={to}&candles=1000&type=price"
http.MIN_INTERVAL.setdefault("datapi.jup.ag", 0.25)

db = sqlite3.connect("data/s5_quant.db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS decisions (run TEXT, ts REAL, account TEXT, strategy TEXT, phase TEXT, mint TEXT, symbol TEXT,
  approved INTEGER, reasons TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS trades (run TEXT, account TEXT, strategy TEXT, phase TEXT, mint TEXT, symbol TEXT, opened REAL, closed REAL,
  size_sol REAL, sol_out REAL, fees_sol REAL, slippage_sol REAL, pnl_sol REAL, pnl_pct REAL, cost_pct REAL, exit_reason TEXT, record TEXT);
CREATE TABLE IF NOT EXISTS fills (run TEXT, account TEXT, mint TEXT, symbol TEXT, opened REAL, ts REAL, kind TEXT, frac REAL,
  sol_out REAL, fee_sol REAL, mark REAL, recovered_sol REAL, size_sol REAL, final INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS equity (run TEXT, ts REAL, eq REAL, balance REAL, open INTEGER);
CREATE INDEX IF NOT EXISTS i_dec ON decisions(run, ts);
""")
CSV = "data/s5_trades.csv"
CSV_COLS = ["run", "account", "strategy", "phase", "symbol", "mint", "opened", "closed", "hold_s", "size_sol", "sol_out", "fees_sol",
            "pnl_sol", "pnl_pct", "exit_reason", "tp_steps", "recovered_sol"]
if not os.path.exists(CSV):
    csv.writer(open(CSV, "w", newline="")).writerow(CSV_COLS)


def dbx(sql, params):
    with LOCK:
        db.execute(sql, params)
        db.commit()


GLOBAL = collections.Counter()
MKT = {"sol_usd": None, "ts": 0}
A = {"balance": G["start_balance_sol"], "start": G["start_balance_sol"], "positions": {}, "closed_n": 0, "pnl": 0.0, "day": None, "day_eq": None,
     "cooldown": {}}
if os.path.exists(ACCT_F):
    try:
        s = json.load(open(ACCT_F))
        if s.get("run") == RUN:
            A.update({k: s[k] for k in A if k in s})
    except Exception as e:
        print("could not restore account:", e)
GATE = C.RiskGate(A["start"], RK["max_open"], RK["daily_stop_pct"], RK["tz_offset_hours"])
if A["day"] is not None:
    GATE.day, GATE.day_eq = A["day"], A["day_eq"]
ENGINE = {m: C.Position.from_state(p["engine"], CARD) for m, p in A["positions"].items()}


def equity():
    return A["balance"] + sum(p["tokens"] * p["last_mark"] + RENT for p in A["positions"].values())


def persist():
    with LOCK:
        for m, e in ENGINE.items():
            if m in A["positions"]:
                A["positions"][m]["engine"] = e.state()
        A["day"], A["day_eq"] = GATE.day, GATE.day_eq
        tmp = ACCT_F + ".tmp"
        json.dump(dict(A, run=RUN), open(tmp, "w"), default=str)
        os.replace(tmp, ACCT_F)


def record(mint, sym, phase, approved, reasons, detail):
    GLOBAL["decisions"] += 1
    if not approved:
        for r in reasons: GLOBAL["reject:" + r.split("(")[0][:50]] += 1
    dbx("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)", (RUN, time.time(), ACCT, "S5", phase, mint, sym, int(approved), ";".join(reasons),
                                                             json.dumps(detail, default=str)))


def update_sol():
    if time.time() - MKT["ts"] < 60:
        return
    d = http.get("https://api.dexscreener.com/latest/dex/pairs/solana/58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2")
    p = ((d or {}).get("pairs") or [None])[0] or (d or {}).get("pair")
    if p and p.get("priceUsd"):
        MKT.update(sol_usd=float(p["priceUsd"]), ts=time.time())


def mark_of(pair):
    if not pair:
        return None
    if (pair.get("quoteToken") or {}).get("address") == WSOL and pair.get("priceNative"):
        return float(pair["priceNative"]) or None
    if pair.get("priceUsd") and MKT["sol_usd"]:
        return float(pair["priceUsd"]) / MKT["sol_usd"]
    return None


def sell_quote(mint, tokens, dec):
    raw = int(tokens * 10 ** dec)
    if raw <= 0:
        return 0.0
    q = sources.jup_quote(mint, WSOL, raw, G["max_slippage_bps"])
    return int(q["outAmount"]) / 1e9 * (1 - LAT) if q and q.get("outAmount") else None


# ------------------------------------------------------------------ the rule, with a readable reason for every skip
def features(tok, now):
    cs = [x for x in tok["c"] if x[0] + 60 <= now]
    if len(cs) < 2:
        return None
    i, x = len(cs) - 1, cs[-1]
    peak = max(y[2] for y in cs[:-1])
    quiet = sum(y[5] for y in cs[:-1] if x[0] - EN["quiet_window_minutes"] * 60 <= y[0] < x[0])
    inflow = S.net_inflow_sol(cs, i, 60) if x[6] == "bonding" else C.quote_reserve_sol(x[4], "graduated") - C.quote_reserve_sol(cs[-2][4], "graduated")
    return {"age_min": round((x[0] - tok["created"]) / 60, 1), "quiet_sol": round(quiet, 3), "wake_vol_sol": round(x[5], 3),
            "inflow_sol": round(inflow or 0, 3), "vs_peak": round(x[4] / peak, 3) if peak else None, "close": x[4], "phase": x[6],
            "candle_age_s": round(now - (x[0] + 60))}


def reasons_of(f):
    R = []
    if f["candle_age_s"] > 90: R.append("no_trade_last_minute")
    if f["age_min"] < EN["min_age_minutes"]: R.append(f"too_young({f['age_min']:.0f}m)")
    if f["quiet_sol"] > EN["quiet_max_volume_sol"]: R.append(f"not_quiet_before({f['quiet_sol']:.1f}_SOL_in_{EN['quiet_window_minutes']}m)")
    if f["wake_vol_sol"] < EN["wake_min_sol"]: R.append(f"wake_volume_low({f['wake_vol_sol']:.1f}<{EN['wake_min_sol']:g}_SOL)")
    if f["inflow_sol"] < EN["wake_min_sol"]: R.append(f"net_buying_low({f['inflow_sol']:.1f}<{EN['wake_min_sol']:g}_SOL)")
    if f["vs_peak"] is None or f["vs_peak"] > EN["max_price_vs_peak"]: R.append(f"not_far_below_peak({(f['vs_peak'] or 0) * 100:.0f}%_of_peak)")
    return R


LAST_CHECK = {}


def evaluate(coin, now):
    mint, sym = coin["mint"], coin.get("symbol") or coin["mint"][:6]
    LAST_CHECK[mint] = now
    sol = MKT["sol_usd"]
    d = http.get(JUP_CHART.format(mint=mint, to=int(now * 1000)))
    raw = (d or {}).get("candles") or []
    if not raw or not sol:
        GLOBAL["no_candles"] += 1
        return
    tok = S.jup_to_tok(raw, sol, (coin.get("created_timestamp") or 0) / 1000, bool(coin.get("complete")))
    f = features(tok, now)
    if not f:
        return
    fired = S.fires_now(SIG, tok, now)
    R = [] if fired else reasons_of(f) or ["rule_not_met"]
    phase = f["phase"]
    if R:
        # only log near-misses (woke up or quiet+deep below peak) so the decision log stays readable
        if f["candle_age_s"] <= 90 and (f["wake_vol_sol"] >= EN["wake_min_sol"] / 2 or (f["quiet_sol"] <= EN["quiet_max_volume_sol"] and (f["vs_peak"] or 1) <= EN["max_price_vs_peak"])):
            record(mint, sym, phase, False, R, f)
        else:
            GLOBAL["quiet_skipped"] += 1
        return
    eq = equity()
    ok, why = GATE.can_enter(now, len(A["positions"]), eq)
    if not ok:
        record(mint, sym, phase, False, [why], f); return
    if mint in A["positions"] or now < A["cooldown"].get(mint, 0):
        record(mint, sym, phase, False, ["already_traded_recently"], f); return
    enter(mint, sym, phase, f, eq)


def enter(mint, sym, phase, f, eq):
    size = C.position_size(eq, EX["stop_loss_pct"] / 100, RK["risk_per_trade_pct"], RK["max_position_pct"] / 100)
    if A["balance"] - size - FEE - RENT < 0:
        record(mint, sym, phase, False, ["insufficient_balance"], f); return
    qb = sources.jup_quote(WSOL, mint, size * 1e9, G["max_slippage_bps"])
    if not (qb and qb.get("outAmount") and int(qb["outAmount"]) > 0):
        record(mint, sym, phase, False, ["no_buy_route"], f); return
    pair = sources.ds_pairs([mint]).get(mint)
    dec = 6
    tokens = int(qb["outAmount"]) / 10 ** dec * (1 - LAT)
    if sell_quote(mint, tokens, dec) is None:
        record(mint, sym, phase, False, ["no_sell_route(honeypot_risk)"], f); return
    mark = mark_of(pair) or f["close"]
    with LOCK:
        if len(A["positions"]) >= RK["max_open"]:
            record(mint, sym, phase, False, ["max_open_positions"], f); return
        A["balance"] -= size + FEE + RENT
        e = C.Position(mark, time.time(), CARD, phase)
        ENGINE[mint] = e
        A["positions"][mint] = dict(mint=mint, symbol=sym, phase=phase, decimals=dec, opened=time.time(), size_sol=size, tokens=tokens, tokens0=tokens,
                                    entry_mark=mark, fill_mark=size / tokens, last_mark=mark, peak_mark=mark, last_ok=time.time(), fees=FEE,
                                    recovered_sol=0.0, sells=[], stage="full", value_sol=None, value_ts=0, signal=f, engine=e.state())
        f = dict(f, size=size, mark=mark, fill_vs_mark_pct=round((size / tokens / mark - 1) * 100, 2))
        record(mint, sym, phase, True, [], f)
        persist()
    log.info("ENTER [%s] S5 %-10s size=%.4f SOL (1%% risk, stop -%d%%) age=%sm quiet=%.2f wake=%.1f inflow=%.1f vs_peak=%.0f%% mark=%.3e",
             phase, sym, size, EX["stop_loss_pct"], f["age_min"], f["quiet_sol"], f["wake_vol_sol"], f["inflow_sol"], (f["vs_peak"] or 0) * 100, mark)


# ------------------------------------------------------------------ selling
def sell(mint, frac_of_original, reason, final):
    with LOCK:
        p = A["positions"].get(mint)
        if not p:
            return False
        tok = p["tokens"] if final else min(p["tokens"], p["tokens0"] * frac_of_original)
        out = sell_quote(mint, tok, p["decimals"])
        if out is None:
            p["exit_fail"] = p.get("exit_fail", 0) + 1
            if not final or (p["exit_fail"] < 10 and reason != "shutdown"):
                return False
            out, reason = 0.0, reason + "+NO_ROUTE_total_loss"
        A["balance"] += out - FEE
        p["fees"] += FEE
        p["tokens"] -= tok
        p["recovered_sol"] += out
        kind = "final" if final else "partial"
        p["sells"].append({"ts": time.time(), "kind": kind, "frac": tok / p["tokens0"], "sol_out": out, "mark": p["last_mark"], "reason": reason})
        dbx("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (RUN, ACCT, mint, p["symbol"], p["opened"], time.time(), kind, tok / p["tokens0"],
                                                                         out, FEE, p["last_mark"], p["recovered_sol"], p["size_sol"], int(final), reason))
        if not final:
            p["stage"] = "tp1_taken"
            log.info("PARTIAL [%s] S5 %-10s %s: sold %.0f%% for %.5f SOL at %.2fx; %.0f%% left on a %d%% trailing stop",
                     p["phase"], p["symbol"], reason, tok / p["tokens0"] * 100, out, p["last_mark"] / p["entry_mark"], p["tokens"] / p["tokens0"] * 100, EX["trail_pct"])
            persist()
            return True
        A["balance"] += RENT
        del A["positions"][mint]
        ENGINE.pop(mint, None)
        A["cooldown"][mint] = time.time() + 60
        closed = time.time()
        pnl = p["recovered_sol"] - p["size_sol"] - p["fees"]
        rec = dict(p, closed=closed, sol_out=p["recovered_sol"], exit_reason=reason, pnl_sol=pnl, pnl_pct=pnl / p["size_sol"] * 100,
                   exit_mark=p["last_mark"], exit_plan=plan(), strategy="S5", account=ACCT)
        rec.pop("engine", None)
        A["closed_n"] += 1
        A["pnl"] += pnl
        dbx("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (RUN, ACCT, "S5", p["phase"], mint, p["symbol"], p["opened"], closed, p["size_sol"], p["recovered_sol"], p["fees"], 0.0, pnl, rec["pnl_pct"],
             p["fees"] / p["size_sol"] * 100, reason, json.dumps(rec, default=str)))
        csv.writer(open(CSV, "a", newline="")).writerow(
            [RUN, ACCT, "S5", p["phase"], p["symbol"], mint, round(p["opened"]), round(closed), round(closed - p["opened"]), round(p["size_sol"], 6),
             round(p["recovered_sol"], 6), round(p["fees"], 6), round(pnl, 6), round(rec["pnl_pct"], 2), reason,
             sum(1 for x in p["sells"] if x["kind"] == "partial"), round(p["recovered_sol"], 6)])
        log.info("EXIT  [%s] S5 %-10s %s pnl=%+.5f SOL (%+.1f%%) hold=%ds bal=%.4f", p["phase"], p["symbol"], reason, pnl, rec["pnl_pct"],
                 closed - p["opened"], A["balance"])
        persist()
        return True


def plan():
    return {"stop_loss_pct": EX["stop_loss_pct"], "take_profit_pct": round((EX["take_profits"][0][0] - 1) * 100), "take_profits": EX["take_profits"],
            "trailing_stop_pct": EX["trail_pct"], "trailing_activate_pct": round((EX["take_profits"][0][0] - 1) * 100), "max_hold_minutes": EX["max_hold_minutes"]}


def monitor_loop():
    last_status, last_eq = 0, 0
    while not STOP.is_set():
        t0 = time.time()
        try:
            update_sol()
            held = list(A["positions"])
            dsp = sources.ds_pairs(held) if held else {}
            for m in held:
                p, e = A["positions"].get(m), ENGINE.get(m)
                if not p or not e:
                    continue
                mark = mark_of(dsp.get(m))
                now = time.time()
                if p.get("pending_exit"):                      # an earlier final sell had no quote: retry it
                    sell(m, 1, p["pending_exit"], True)
                    continue
                if mark:
                    p["last_mark"], p["last_ok"] = mark, now
                    p["peak_mark"] = max(p["peak_mark"], mark)
                    ph = "graduated" if (dsp.get(m) or {}).get("dexId") not in (None, "pumpfun") else "bonding"
                    before = e.state()
                    for frac, px, why in e.tick(now, mark, ph):
                        if e.closed:
                            if not sell(m, 1, why, True):
                                p["pending_exit"] = why
                        elif not sell(m, frac, why, False):
                            ENGINE[m] = C.Position.from_state(before, CARD)   # partial not filled: undo, retry on the next tick
                elif now - p["last_ok"] > EX["price_lost_seconds"]:
                    e.closed = True
                    if not sell(m, 1, "price_data_lost", True): p["pending_exit"] = "price_data_lost"
                elif now - p["opened"] >= EX["max_hold_minutes"] * 60:
                    e.closed = True
                    if not sell(m, 1, "time_exit", True): p["pending_exit"] = "time_exit"
                if m in A["positions"] and now - p["value_ts"] > 15:
                    v = sell_quote(m, p["tokens"], p["decimals"])
                    p["value_ts"] = now
                    if v is not None: p["value_sol"] = v
            eq = equity()
            GATE.roll(time.time(), eq)
            if time.time() - last_eq > 30:
                last_eq = time.time()
                dbx("INSERT INTO equity VALUES (?,?,?,?,?)", (RUN, time.time(), eq, A["balance"], len(A["positions"])))
            if time.time() - last_status > 60:
                last_status = time.time()
                log.info("STATUS S5: eq=%.4f open=%d closed=%d | bal=%.4f day=%+.2f%% decisions=%d checked=%d", eq, len(A["positions"]), A["closed_n"],
                         A["balance"], GATE.day_pnl_pct(eq), GLOBAL["decisions"], GLOBAL["checked"])
                persist()
            write_state(eq)
        except Exception as ex:
            log.exception("monitor error %s", ex)
        STOP.wait(max(0.5, G["monitor_interval_seconds"] - (time.time() - t0)))


CYCLE = {}


def write_state(eq):
    with LOCK:
        hs = []
        for m, p in A["positions"].items():
            e = ENGINE.get(m)
            h = {k: p.get(k) for k in ("mint", "symbol", "phase", "opened", "size_sol", "tokens", "tokens0", "decimals", "entry_mark", "last_mark",
                                        "peak_mark", "stage", "recovered_sol", "fees", "value_sol", "value_ts")}
            if e:
                h["stop_mark"] = e._stop_px()
                h["tp_done"] = e.tp_done
            hs.append(h)
        halted = GATE.day_pnl_pct(eq) <= -RK["daily_stop_pct"] if GATE.day is not None else False
        st = {"run": RUN, "ts": time.time(), "pid": os.getpid(), "account": ACCT, "equity": eq, "balance": A["balance"], "start": A["start"],
              "open": len(hs), "closed_n": A["closed_n"], "pnl": A["pnl"], "holdings": hs, "sol_usd": MKT["sol_usd"], "plan": plan(),
              "day_pnl_pct": GATE.day_pnl_pct(eq), "halted": f"daily auto-stop: day at {GATE.day_pnl_pct(eq):.1f}% (limit -{RK['daily_stop_pct']:g}%)" if halted else None,
              "counters": dict(GLOBAL.most_common(40)), "last_cycle": CYCLE, "config": {"entry": EN, "exit": EX, "risk": RK, "strategy": G["strategy"]}}
    tmp = STATE_F + ".tmp"
    json.dump(st, open(tmp, "w"), default=str)
    os.replace(tmp, STATE_F)


def scan_loop():
    while not STOP.is_set():
        t0 = time.time()
        try:
            update_sol()
            coins = {}
            for page in range(G["scan_list_pages"]):
                d = http.get(f"{pumpfun.PF}/coins?offset={page * 50}&limit=50&sort=last_trade_timestamp&order=DESC&includeNsfw=false") or []
                for c in d if isinstance(d, list) else []:
                    if c.get("mint"): coins[c["mint"]] = c
            now = time.time()
            due = []
            for m, c in coins.items():
                age = now - (c.get("created_timestamp") or now * 1000) / 1000
                if age < EN["min_age_minutes"] * 60 or c.get("is_banned"):
                    GLOBAL["young_skipped"] += 1; continue
                ath, mc = c.get("ath_market_cap"), c.get("usd_market_cap")
                if ath and mc and mc > ath * (EN["max_price_vs_peak"] + 0.1):       # cheap pre-filter: nowhere near 50% below its peak
                    GLOBAL["near_peak_skipped"] += 1; continue
                if now - LAST_CHECK.get(m, 0) < G["recheck_seconds"]:
                    continue
                due.append(c)
            if len(LAST_CHECK) > 20000:
                for k in [k for k, v in LAST_CHECK.items() if now - v > 3600]: LAST_CHECK.pop(k, None)
            for c in due:
                if STOP.is_set(): break
                GLOBAL["checked"] += 1
                evaluate(c, time.time())
            CYCLE.update(ts=time.time(), found=len(coins), evaluated=len(due), secs=round(time.time() - t0, 1))
            log.info("cycle: %d recently traded coins, %d checked (%.0fs)", len(coins), len(due), time.time() - t0)
        except Exception as ex:
            log.exception("scan error %s", ex)
        STOP.wait(max(2, G["scan_interval_seconds"] - (time.time() - t0)))


log.info("S5 QUANT paper run %s (pid %d); strategy=%s; resumed=%s balance=%.4f open=%d; risk %.1f%%/trade, max %d open, daily stop -%g%%; "
         "SL -%d%%, TP %s, trail %d%% after TP1, max hold %d min", RUN, os.getpid(), G["strategy"], bool(A["positions"] or A["closed_n"]), A["balance"],
         len(A["positions"]), RK["risk_per_trade_pct"], RK["max_open"], RK["daily_stop_pct"], EX["stop_loss_pct"], EX["take_profits"], EX["trail_pct"],
         EX["max_hold_minutes"])
if GATE.day is None:
    GATE.day_start(time.time(), equity())
ths = [threading.Thread(target=f, daemon=True) for f in (scan_loop, monitor_loop)]
for t in ths: t.start()
TERM = threading.Event()
signal.signal(signal.SIGTERM, lambda *a: TERM.set())
try:
    while not TERM.is_set():
        time.sleep(1)
except KeyboardInterrupt:
    pass
STOP.set()
time.sleep(2)
persist()
log.info("S5 stopped; %d open positions kept for the next start", len(A["positions"]))
