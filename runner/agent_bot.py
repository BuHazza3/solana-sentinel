#!/usr/bin/env python3
"""MULTI-AGENT PAPER BOT (PAPER ONLY: no wallet, no keys, no transactions). One process per bot:
  python3 agent_bot.py --bot V1     (v1_bot.toml)     python3 agent_bot.py --bot S5     (s5_bot.toml)     python3 agent_bot.py --bot S6     (s6_bot.toml)
  python3 agent_bot.py --bot S7     (s7_bot.toml: copy-trade only, copies the best-ranked watched wallets, see s7_rank.py)
Each bot has ONE 1 SOL paper bankroll shared by its sub-agents (agents.toml). Every sub-agent keeps its own entry logic and
its own exits from the bot it came from (V1_SNIPER, ORIGINAL/REVISED, M4, S5); the host applies the bot's sizing and limits.
Switches (data/agent_toggles.json, written only by the app's authenticated command channel): off = no new entries for that
sub-agent (open positions are still managed to their exits). A restart resumes the same paper account and open positions.
Live data: pump.fun API, DexScreener, RugCheck, Jupiter quotes/prices/charts, public RPC (watched wallets). Fills are simulated
from real Jupiter quotes + latency slippage + network fees + token-account rent."""
import argparse, collections, csv, json, logging, os, queue, signal, sqlite3, sys, threading, time, tomllib
from bot import sources, rugfilter, scoring, riskscore, http, pumpfun
from bot.copytrade import CopyWatcher
import s5_core as C
import s5_signals as S5
import agent_exits as EXI
import agent_toggles
import s7_rank

ap = argparse.ArgumentParser()
ap.add_argument("--bot", required=True, choices=["V1", "S5", "S6", "S7"])
ap.add_argument("--new-run", action="store_true")
args = ap.parse_args()
BOT = args.bot
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
BCFG = tomllib.load(open(f"{BOT.lower()}_bot.toml", "rb"))
ACFG = tomllib.load(open("agents.toml", "rb"))
V1CFG = tomllib.load(open("config.toml", "rb"))
assert BCFG["general"]["paper"] is True and ACFG["general"]["paper"] is True and V1CFG["mode"]["paper"] is True, "paper only"
G, RK = ACFG["general"], BCFG["risk"]
AG = {a: dict(ACFG["agent"][a], key=a) for a in BCFG["agents"]}          # the sub-agents this bot hosts
LAT, RENT = G["latency_slippage_bps"] / 1e4, G["ata_rent_sol"]
FEE = {a: ACFG["fees"][AG[a]["fees"]]["sol"] for a in AG}
WSOL = sources.WSOL
os.makedirs("data", exist_ok=True)
P = f"data/{BOT.lower()}_bot"
STATE_F, ACCT_F, RUN_F = P + "_state.json", P + "_account.json", P + "_run.txt"
if args.new_run or not os.path.exists(RUN_F):
    open(RUN_F, "w").write(time.strftime(f"{BOT}%Y%m%d-%H%M%S"))
    if os.path.exists(ACCT_F): os.replace(ACCT_F, ACCT_F + ".prev")
RUN = open(RUN_F).read().strip()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-8s %(message)s", datefmt="%H:%M:%S",
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(f"{P}_{RUN}.log")])
logging.getLogger("http").setLevel(logging.WARNING)
log = logging.getLogger(BOT.lower())
LOCK = threading.RLock()
STOP = threading.Event()
http.MIN_INTERVAL.setdefault("datapi.jup.ag", 0.25)
TOG = agent_toggles.Watch(BOT, BCFG["agents"])

db = sqlite3.connect(P + ".db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS decisions (run TEXT, ts REAL, bot TEXT, agent TEXT, phase TEXT, mint TEXT, symbol TEXT, approved INTEGER, reasons TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS trades (run TEXT, bot TEXT, agent TEXT, phase TEXT, mint TEXT, symbol TEXT, opened REAL, closed REAL, size_sol REAL, sol_out REAL,
  fees_sol REAL, pnl_sol REAL, pnl_pct REAL, exit_reason TEXT, record TEXT);
CREATE TABLE IF NOT EXISTS fills (run TEXT, bot TEXT, agent TEXT, mint TEXT, symbol TEXT, opened REAL, ts REAL, kind TEXT, frac REAL, sol_out REAL, fee_sol REAL,
  mark REAL, recovered_sol REAL, size_sol REAL, final INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS equity (run TEXT, ts REAL, eq REAL, balance REAL, open INTEGER);
CREATE TABLE IF NOT EXISTS toggles (run TEXT, ts REAL, rev INTEGER, state TEXT);
CREATE INDEX IF NOT EXISTS i_dec ON decisions(run, ts);
CREATE INDEX IF NOT EXISTS i_dec_a ON decisions(run, agent, ts);
""")
CSV = P + "_trades.csv"
if not os.path.exists(CSV):
    csv.writer(open(CSV, "w", newline="")).writerow(["run", "bot", "agent", "phase", "symbol", "mint", "opened", "closed", "hold_s", "size_sol", "sol_out",
                                                     "fees_sol", "pnl_sol", "pnl_pct", "exit_reason", "partials", "wallets"])


def dbx(sql, params):
    with LOCK:
        db.execute(sql, params)
        db.commit()


GLOBAL = collections.Counter()
AST = {a: collections.Counter() for a in AG}          # per sub-agent counters (signals / approved / rejected)
LASTD = {}                                             # agent -> last decision text
MKT = {"sol_usd": None, "ts": 0}
REG = {"ok": True, "why": "init", "pc": {}}
CYCLE = {}

# ------------------------------------------------------------------ the shared bankroll (persisted; restart resumes)
A = {"balance": BCFG["general"]["start_balance_sol"], "start": BCFG["general"]["start_balance_sol"], "positions": {}, "traded": [], "closed_n": 0,
     "pnl": 0.0, "day": None, "day_eq": None, "cooldown": {}, "agent_pnl": {}, "agent_n": {}, "wallet_own": {}}
if os.path.exists(ACCT_F):
    try:
        s = json.load(open(ACCT_F))
        if s.get("run") == RUN:
            A.update({k: s[k] for k in A if k in s})
    except Exception as e:
        print("could not restore account:", e)
TRADED = set(A["traded"])
GATE = C.RiskGate(A["start"], RK["max_open"], RK.get("daily_stop_pct", 1e9), RK.get("tz_offset_hours", 4))
if A["day"] is not None:
    GATE.day, GATE.day_eq = A["day"], A["day_eq"]


def equity():
    return A["balance"] + sum(p["tokens"] * p["last_mark"] + RENT for p in A["positions"].values())


def persist():
    with LOCK:
        A["traded"] = sorted(TRADED)
        A["day"], A["day_eq"] = GATE.day, GATE.day_eq
        tmp = ACCT_F + ".tmp"
        json.dump(dict(A, run=RUN), open(tmp, "w"), default=str)
        os.replace(tmp, ACCT_F)


def record(agent, mint, sym, phase, approved, reasons, detail):
    GLOBAL["decisions"] += 1
    AST[agent]["approved" if approved else "rejected"] += 1
    if not approved:
        for r in reasons: GLOBAL["reject:" + r.split("(")[0][:50]] += 1
    LASTD[agent] = {"ts": time.time(), "symbol": sym, "approved": approved, "reasons": reasons[:3]}
    dbx("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)", (RUN, time.time(), BOT, agent, phase, mint, sym, int(approved), ";".join(reasons),
                                                             json.dumps(detail, default=str)))


def enabled(agent):
    return agent in AG and TOG.on(agent)


# ------------------------------------------------------------------ bot-level sizing and limits (shared bankroll)
def size_for(agent):
    X = AG[agent]["exit"]
    eq = equity()
    if RK["sizing"] == "v1":
        s = min(A["balance"] * RK["position_pct"] / 100, RK["max_position_sol"], A["balance"] - RK["reserve_sol"])
        return s if s >= RK["min_position_sol"] else 0.0
    if RK["sizing"] == "tier":                      # S7: size by the copied wallet's rank tier (the agent's tier)
        return s7_rank.size_for_tier(AG[agent].get("tier"), A["balance"], RK)
    s = C.position_size(eq, X["stop_loss_pct"] / 100, RK["risk_per_trade_pct"], RK["max_position_pct"] / 100)
    return s if A["balance"] - s - FEE[agent] - RENT > 0 else 0.0


def bot_gate(agent, mint):
    R = []
    if not TOG.on(agent): R.append("switched_off")
    ok, why = GATE.can_enter(time.time(), len(A["positions"]), equity())
    if not ok: R.append(why)
    if mint in A["positions"]: R.append("already_holding")
    if AG[agent]["kind"] != "revival" and mint in TRADED: R.append("already_traded(no_averaging)")
    if AG[agent]["kind"] == "revival" and time.time() < A["cooldown"].get(mint, 0): R.append("already_traded_recently")
    return R


# ------------------------------------------------------------------ market helpers
def update_sol():
    if time.time() - MKT["ts"] < 30:
        return
    d = http.get("https://api.dexscreener.com/latest/dex/pairs/solana/58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2")
    p = ((d or {}).get("pairs") or [None])[0] or (d or {}).get("pair")
    if not p or not p.get("priceUsd"):
        REG.update(ok=False, why="regime_data_unavailable")
        return
    MKT.update(sol_usd=float(p["priceUsd"]), ts=time.time())
    pc = p.get("priceChange") or {}
    rg = (AG.get("C_GRAD") or ACFG["agent"]["C_GRAD"])["regime"]
    why = []
    if (pc.get("h1") or 0) < rg["sol_h1_min_pct"]: why.append(f"SOL_h1={pc.get('h1')}%")
    if (pc.get("h6") or 0) < rg["sol_h6_min_pct"]: why.append(f"SOL_h6={pc.get('h6')}%")
    if (pc.get("h24") or 0) < rg["sol_h24_min_pct"]: why.append(f"SOL_h24={pc.get('h24')}%")
    REG.update(ok=not why, why=",".join(why) or "ok", pc=pc, ts=time.time())


def valid_px(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and 0 < x < float("inf")


def mark_of(pair):
    if not pair:
        return None
    if (pair.get("quoteToken") or {}).get("address") == WSOL and pair.get("priceNative"):
        return float(pair["priceNative"]) or None
    if pair.get("priceUsd") and MKT["sol_usd"]:
        return float(pair["priceUsd"]) / MKT["sol_usd"]
    return None


_route_cache = {}


def route_check(mint, size_sol):
    key = (mint, round(size_sol, 3))
    c = _route_cache.get(key)
    if c and time.time() - c["ts"] < 15:
        return c
    qb = sources.jup_quote(WSOL, mint, size_sol * 1e9, G["max_slippage_bps"])
    r = {"ts": time.time(), "size": size_sol, "buy_ok": False, "sell_ok": False}
    if qb and qb.get("outAmount") and int(qb["outAmount"]) > 0:
        r.update(buy_ok=True, tokens_raw=int(qb["outAmount"]), buy_impact_pct=float(qb.get("priceImpactPct") or 0) * 100,
                 route=[x["swapInfo"].get("label") for x in qb.get("routePlan", [])])
        qs = sources.jup_quote(mint, WSOL, r["tokens_raw"], G["max_slippage_bps"])
        if qs and qs.get("outAmount") and int(qs["outAmount"]) > 0:
            r.update(sell_ok=True, sell_impact_pct=float(qs.get("priceImpactPct") or 0) * 100, round_trip_loss_pct=(1 - int(qs["outAmount"]) / 1e9 / size_sol) * 100)
    _route_cache[key] = r
    if len(_route_cache) > 4000: _route_cache.clear()
    return r


def sell_quote(mint, tokens, dec):
    raw = int(tokens * 10 ** dec)
    if raw <= 0:
        return 0.0
    q = sources.jup_quote(mint, WSOL, raw, G["max_slippage_bps"])
    return int(q["outAmount"]) / 1e9 * (1 - LAT) if q and q.get("outAmount") else None


# ------------------------------------------------------------------ opening a position (any sub-agent)
def open_position(agent, mint, sym, phase, size, tokens, dec, liq, extra, detail):
    """Books the paper buy. Cost basis = the real Jupiter fill price (incl. latency slippage)."""
    with LOCK:
        R = bot_gate(agent, mint)
        if R:
            record(agent, mint, sym, phase, False, R, detail); return None
        fill = size / tokens if tokens > 0 else None
        if not valid_px(fill):
            record(agent, mint, sym, phase, False, [f"invalid_entry_price({fill})"], detail); return None
        X = AG[agent]["exit"]
        basis = extra.pop("entry_basis", None)     # S5 card: exits measured from the DEX mark, as in s5_quant.py
        basis = basis if valid_px(basis) else fill
        A["balance"] -= size + FEE[agent] + RENT
        now = time.time()
        p = dict(agent=agent, mint=mint, symbol=sym, phase=phase, decimals=dec, opened=now, size_sol=size, tokens=tokens, tokens0=tokens,
                 entry_mark=basis, fill_mark=fill, last_mark=basis, peak_mark=basis, last_ok=now, entry_liq=liq, last_liq=liq, fees=FEE[agent], recovered_sol=0.0,
                 sells=[], stage="full", value_sol=None, value_ts=0, plan=EXI.plan(X), **dict({"wallets": []}, **extra))
        if X["engine"] == "card":
            p["engine"] = C.Position(basis, now, EXI.card_of(X), phase).state()
        if X["engine"] == "m4":
            p["stop_mark"] = EXI.m4_stop(p, X)
        A["positions"][mint] = p
        if AG[agent]["kind"] != "revival":
            TRADED.add(mint)
        AST[agent]["entered"] += 1
        record(agent, mint, sym, phase, True, [], dict(detail, size=size, fill=fill))
        persist()
    log.info("ENTER [%s] %s %-10s %-10s size=%.4f SOL fill=%.3e %s", phase, BOT, agent, sym, size, fill,
             " ".join(f"{k}={v}" for k, v in detail.items() if k in ("signal", "rug_score", "cost_pct", "wallets", "tier", "age_min", "vs_peak") and v is not None))
    return p


# ------------------------------------------------------------------ selling
def sell(mint, frac, reason, final):
    """frac = fraction of the ORIGINAL tokens (partials); final sells everything left."""
    with LOCK:
        p = A["positions"].get(mint)
        if not p:
            return False
        ag = p["agent"]
        tok = p["tokens"] if final else min(p["tokens"], p["tokens0"] * frac)
        out = sell_quote(mint, tok, p["decimals"])
        if out is None:
            p["exit_fail"] = p.get("exit_fail", 0) + 1
            if not final or (p["exit_fail"] < 10 and reason != "shutdown"):
                return False
            out, reason = 0.0, reason + "+NO_ROUTE_total_loss"
        fee = FEE[ag]
        A["balance"] += out - fee
        p["fees"] += fee
        p["tokens"] -= tok
        p["recovered_sol"] += out
        kind = "final" if final else ("initials" if reason == "initials_pulled" else "partial")
        p["sells"].append({"ts": time.time(), "kind": kind, "frac": tok / p["tokens0"], "sol_out": out, "mark": p["last_mark"], "reason": reason})
        dbx("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (RUN, BOT, ag, mint, p["symbol"], p["opened"], time.time(), kind, tok / p["tokens0"], out, fee,
                                                                           p["last_mark"], p["recovered_sol"], p["size_sol"], int(final), reason))
        if not final:
            log.info("PARTIAL [%s] %s %-10s %-10s %s: sold %.0f%% for %.5f SOL at %.2fx", p["phase"], BOT, ag, p["symbol"], reason, tok / p["tokens0"] * 100, out,
                     p["last_mark"] / p["entry_mark"])
            persist()
            return True
        A["balance"] += RENT
        del A["positions"][mint]
        if AG[ag]["kind"] == "revival":
            A["cooldown"][mint] = time.time() + 60
        closed = time.time()
        pnl = p["recovered_sol"] - p["size_sol"] - p["fees"]
        rec = dict(p, closed=closed, sol_out=p["recovered_sol"], exit_reason=reason, pnl_sol=pnl, pnl_pct=pnl / p["size_sol"] * 100, exit_mark=p["last_mark"],
                   exit_plan=p["plan"], bot=BOT)
        rec.pop("engine", None)
        A["closed_n"] += 1
        A["pnl"] += pnl
        A["agent_pnl"][ag] = A["agent_pnl"].get(ag, 0.0) + pnl
        A["agent_n"][ag] = A["agent_n"].get(ag, 0) + 1
        if ag in S7_AGENTS and p.get("wallets"):        # S7 re-ranks the wallet from its own closed trades
            A["wallet_own"].setdefault(p["wallets"][0], []).append({"ts": round(closed), "ret": round(pnl / p["size_sol"], 4), "sym": p["symbol"]})
        dbx("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (RUN, BOT, ag, p["phase"], mint, p["symbol"], p["opened"], closed, p["size_sol"],
                                                                          p["recovered_sol"], p["fees"], pnl, rec["pnl_pct"], reason, json.dumps(rec, default=str)))
        csv.writer(open(CSV, "a", newline="")).writerow([RUN, BOT, ag, p["phase"], p["symbol"], mint, round(p["opened"]), round(closed), round(closed - p["opened"]),
                                                         round(p["size_sol"], 6), round(p["recovered_sol"], 6), round(p["fees"], 6), round(pnl, 6),
                                                         round(rec["pnl_pct"], 2), reason, sum(1 for x in p["sells"] if x["kind"] != "final"), "|".join(p["wallets"])])
        log.info("EXIT  [%s] %s %-10s %-10s %s pnl=%+.5f SOL (%+.1f%%) hold=%ds bal=%.4f", p["phase"], BOT, ag, p["symbol"], reason, pnl, rec["pnl_pct"],
                 closed - p["opened"], A["balance"])
        persist()
        return True


def pull_initials(mint):
    """M4: sell just enough to get back the SOL put in + all fees (buy, this sell, and the final sell)."""
    p = A["positions"][mint]
    fee = FEE[p["agent"]]
    target = p["size_sol"] + p["fees"] + 2 * fee
    est = p["tokens"] * p["last_mark"] * (1 - LAT)
    if est <= 0:
        return False
    frac = min(0.9, target / est * 1.03)
    out = sell_quote(mint, p["tokens"] * frac, p["decimals"])
    if out is None:
        return False
    if out < target and frac < 0.9:
        frac = min(0.9, frac * target / max(out, 1e-9) * 1.02)
    if not sell(mint, frac * p["tokens"] / p["tokens0"], "initials_pulled", False):
        return False
    EXI.m4_after_initials(p, AG[p["agent"]]["exit"])
    persist()
    return True


# ------------------------------------------------------------------ pipeline 1: DexScreener/pump.fun candidates -> V1 snipe/scanner, C, LS
COINS, SEEN = {}, {}
_pump_cache = {}


def build_cand(mint, pair, source, coin=None):
    """pump_sentinel.py build_cand."""
    coin = coin or COINS.get(mint)
    if not pair:
        return None
    if not pair.get("liquidity"):
        rep = sources.rc_report(mint)
        pair["liquidity"] = {"usd": (rep or {}).get("totalMarketLiquidity") or 0, "_from": "rugcheck"}
    tx = (pair.get("txns") or {}).get("m5") or {}
    if tx.get("buys", 0) < 5 and not source.startswith("whale"):
        GLOBAL["skipped_inactive"] += 1
        return None
    liq = pair["liquidity"]["usd"] or 0
    report = sources.rc_report(mint) if liq >= 3000 or source.startswith("whale") else None
    ev = pumpfun.is_pump(mint, report, pair, coin)
    if not ev:
        GLOBAL["not_pump_token"] += 1
        return None
    phase = pumpfun.phase_of(report, pair, coin)
    native = float(pair.get("priceNative") or 0)
    if (pair.get("quoteToken") or {}).get("address") != WSOL:
        native = float(pair.get("priceUsd") or 0) / MKT["sol_usd"] if MKT["sol_usd"] else 0
    sc, parts = scoring.score(pair, None)
    return dict(mint=mint, symbol=(pair.get("baseToken") or {}).get("symbol", "?"), pair=pair, pair_ts=time.time(), report=report, mark_sol=native, liq=liq,
                score=sc, parts=parts, age=scoring.age_minutes(pair), source=source, coin=coin, phase=phase, pump_evidence=ev,
                progress=pumpfun.curve_progress_from_report(report) if phase == "bonding" else None)


def pump_data(c):
    k = c["mint"]
    hit = _pump_cache.get(k)
    if hit and time.time() - hit[0] < 300:
        d = hit[1]
    else:
        creator = (c.get("coin") or {}).get("creator") or (c.get("report") or {}).get("creator")
        d = {"creator": creator, "dev_history": pumpfun.creator_history(creator) if creator else None,
             "launch": pumpfun.launch_analysis(k, creator) if creator else {}}
        _pump_cache[k] = (time.time(), d)
    return dict(d, phase=c["phase"], coin=c.get("coin"), progress=c.get("progress"), age_min=c.get("age"))


def sig_v1(c):
    s, p = V1CFG["strategy"], c["parts"]
    if c["age"] is not None and c["age"] <= s["snipe_max_age_minutes"] and p["b5"] >= s["snipe_min_buys_5m"] and p["b5"] >= s["snipe_min_buy_sell_ratio"] * max(1, p["s5"]):
        return "snipe"
    if c["score"] >= s["scanner_min_score"] and p["m5"] > 0 and p["b5"] > p["s5"]:
        return "scanner"
    return None


def sig_c_grad(c, key="C_GRAD"):
    S, p, pr = AG[key], c["parts"], c["pair"]
    if c["phase"] != "graduated":
        return False
    tx = pr.get("txns") or {}
    b1, s1 = (tx.get("h1") or {}).get("buys", 0), (tx.get("h1") or {}).get("sells", 0)
    vol = (pr.get("volume") or {}).get("h1") or 0
    return (c["liq"] >= S["min_liquidity_usd"] and (c["age"] or 0) >= S["min_age_minutes"] and vol / max(1, c["liq"]) >= S["min_vol_liq_h1"]
            and b1 >= S["min_buy_sell_ratio_h1"] * max(1, s1) and p["m5"] > 0 and 0 < p["h1"] < S["max_h1_pct"] and p["b5"] > p["s5"] and c["score"] >= S["min_score"])


def sig_ls(c):
    ls, p, prog = AG["LS"], c["parts"], c.get("progress")
    return (c["phase"] == "bonding" and c["age"] is not None and c["age"] <= ls["max_age_minutes"] and prog is not None
            and ls["min_progress_pct"] <= prog <= ls["max_progress_pct"] and p["b5"] >= ls["min_buys_5m"] and p["b5"] >= ls["min_buy_sell_ratio"] * max(1, p["s5"]))


def mcap_usd(c):
    """Market cap in USD: DexScreener marketCap/fdv, else pump.fun's 1B supply x price."""
    pr = c.get("pair") or {}
    for k in ("marketCap", "fdv"):
        v = pr.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    if c.get("mark_sol") and MKT["sol_usd"]:
        return c["mark_sol"] * 1e9 * MKT["sol_usd"]
    return None


def classic_enter(agent, c, meta):
    """pump_sentinel.py decide_and_enter, per sub-agent (kind v1 / c / ls); sizing + limits from the host bot."""
    S = AG[agent]
    kind, X = S["kind"], S["exit"]
    AST[agent]["signals"] += 1
    pair, report = c["pair"], c["report"]
    R, checks = [], []
    if kind == "c" and S.get("use_regime_gate") and not REG["ok"]: R.append("F_regime_cash:" + REG["why"])
    R += bot_gate(agent, c["mint"])
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    min_liq = S.get("min_liquidity_usd", V1CFG["filter"]["min_liquidity_usd"]) if kind == "v1" else S["min_liquidity_usd"]   # S6 copy agents set their own floor; V1 agents use config.toml
    if liq < min_liq: R.append(f"liquidity_below_min({liq:.0f}<{min_liq})")
    if time.time() - c["pair_ts"] > 90: R.append("stale_market_data")
    if report is None and kind != "v1": R.append("security_data_unavailable")
    mc = None
    if "max_mcap_usd" in S or "min_mcap_usd" in S:        # S6 agents only: pump.fun market-cap band (V1/S5 agents have no such keys)
        mc = mcap_usd(c)
        hi = min(S.get("max_mcap_usd", 1e18), RK.get("max_mcap_usd", 1e18))
        if mc is None: R.append("mcap_unknown")
        elif mc > hi: R.append(f"mcap_above_cap(${mc:,.0f}>${hi:,.0f})")
        elif mc < S.get("min_mcap_usd", 0): R.append(f"mcap_below_min(${mc:,.0f}<${S['min_mcap_usd']:,.0f})")
        meta = dict(meta, mcap_usd=mc and round(mc))
    if R:
        record(agent, c["mint"], c["symbol"], c["phase"], False, R, {"stage": "pre", **meta}); return
    score, comp = None, None
    if kind == "v1":
        cfg = V1CFG if "min_liquidity_usd" not in S else dict(V1CFG, filter=dict(V1CFG["filter"], min_liquidity_usd=S["min_liquidity_usd"]))
        ok, rr_, info = rugfilter.check(pair, report, cfg)
        if not ok:
            record(agent, c["mint"], c["symbol"], c["phase"], False, ["v1_filter:" + r for r in rr_], dict(info, **meta)); return
        comp = info
    else:
        pre, pv, _, _ = riskscore.score(pair, report, None, S["crit"], c["phase"])
        if pv or pre - 20 > S["max_rug_score"]:
            record(agent, c["mint"], c["symbol"], c["phase"], False, ["veto:" + v for v in pv] + ([f"rug_score({pre - 20}+>{S['max_rug_score']})"] if pre - 20 > S["max_rug_score"] else []),
                   {"stage": "security_pre", **meta}); return
    size = size_for(agent)
    if size <= 0:
        record(agent, c["mint"], c["symbol"], c["phase"], False, ["insufficient_balance"], meta); return
    route = route_check(c["mint"], size)
    if not route["buy_ok"]: R.append("no_buy_route")
    if kind != "v1":
        score, vetoes, comp, unc = riskscore.pump_score(pair, report, route, S["crit"], pump_data(c))
        R += ["veto:" + v for v in vetoes]
        if score > S["max_rug_score"]: R.append(f"rug_score({score}>{S['max_rug_score']})")
    elif not route.get("sell_ok"):
        R.append("no_sell_route")
    cost = None
    if route.get("sell_ok"):
        cost = max(0.0, route["round_trip_loss_pct"]) + 2 * LAT * 100 + 2 * FEE[agent] / size * 100
    elif not any(r.startswith(("veto:no_sell_route", "no_sell_route")) for r in R):
        R.append("no_sell_route")
    rr = None
    if kind == "c" and cost is not None:
        tp_pct = X["take_profit_pct"] if "take_profit_pct" in X else (X["take_profits"][0][0] - 1) * 100
        rr = (tp_pct - cost) / (X["stop_loss_pct"] + cost)
        if rr < S["min_rr"]: R.append(f"rr_after_costs({rr:.2f}<{S['min_rr']})")
    dec = ((report or {}).get("token") or {}).get("decimals")
    if dec is None and not R: R.append("no_decimals")
    detail = {"rug_score": score, "cost_pct": cost and round(cost, 2), "rr": rr and round(rr, 2), "liq": liq, "regime": REG.get("why"), "components": comp, **meta}
    if R:
        record(agent, c["mint"], c["symbol"], c["phase"], False, R, detail); return
    tokens = route["tokens_raw"] / 10 ** dec * (1 - LAT)
    open_position(agent, c["mint"], c["symbol"], c["phase"], size, tokens, dec, liq,
                  {"cost_pct_est": cost, "rug_score": score, "source": meta.get("source"), "signal": meta.get("signal"), "wallets": list(meta.get("wallets") or [])}, detail)


def dex_loop():
    """pump_sentinel.py discovery_loop (every 30 s, re-evaluate a token at most every 5 min)."""
    while not STOP.is_set():
        t0 = time.time()
        try:
            update_sol()
            want = [a for a in ("V1_SNIPE", "V1_SCANNER", "C_GRAD", "LS", "S6_CGRAD") if enabled(a)]
            if want:
                found = {}
                for m, (tag, coin) in pumpfun.discover().items():
                    COINS[m] = coin
                    found[m] = tag
                for src in (sources.ds_discover, sources.jup_discover, sources.rc_discover):
                    for m, tag in src().items():
                        if m.endswith("pump"): found.setdefault(m, tag)
                fresh = [m for m in found if time.time() - SEEN.get(m, 0) > 300]
                pairs = sources.ds_pairs(fresh) if fresh else {}
                for m in fresh:
                    if STOP.is_set(): break
                    SEEN[m] = time.time()
                    c = build_cand(m, pairs.get(m), found[m])
                    if c is None:
                        continue
                    GLOBAL["dex_tokens_scanned"] += 1
                    meta = {"source": c["source"]}
                    v = sig_v1(c)
                    if v == "snipe" and enabled("V1_SNIPE"): classic_enter("V1_SNIPE", c, dict(meta, signal="snipe"))
                    if v == "scanner" and enabled("V1_SCANNER"): classic_enter("V1_SCANNER", c, dict(meta, signal="scanner"))
                    if enabled("C_GRAD") and sig_c_grad(c): classic_enter("C_GRAD", c, dict(meta, signal="momentum_confirmation"))
                    if enabled("LS") and sig_ls(c): classic_enter("LS", c, dict(meta, signal="new_launch"))
                    if enabled("S6_CGRAD") and sig_c_grad(c, "S6_CGRAD"): classic_enter("S6_CGRAD", c, dict(meta, signal="momentum_confirmation"))
                CYCLE["dex"] = {"ts": time.time(), "found": len(found), "evaluated": len(fresh), "secs": round(time.time() - t0, 1), "regime": REG["why"]}
                log.info("cycle dex: %d pump candidates, %d evaluated, regime=%s SOL=$%s (%.0fs)", len(found), len(fresh), REG["why"], MKT["sol_usd"], time.time() - t0)
        except Exception as e:
            log.exception("dex loop error %s", e)
        STOP.wait(max(2, 30 - (time.time() - t0)))


# ------------------------------------------------------------------ pipeline 2: watched wallets -> COPY
LEAD_SELL = collections.defaultdict(dict)
LEAD_BUY = collections.defaultdict(dict)        # mint -> {wallet label: last buy ts} (S6 confluence)
CONFIRM_Q = []                                  # S6_CONFIRM: pending price confirmations
COPY_AGENTS = ("COPY", "S6_COPY", "S6_CONFL", "S6_CONFIRM", "S7_TOP", "S7_NEXT")
S7_AGENTS = ("S7_TOP", "S7_NEXT")
S7_TIER_AGENT = {1: "S7_TOP", 2: "S7_NEXT"}
SIGQ = queue.Queue()
WALLETS_F = os.path.join(HERE, "wallets.toml")


def load_wallets():
    try:
        return tomllib.load(open(WALLETS_F, "rb")).get("follow") or []
    except Exception as e:
        log.warning("wallets.toml unreadable: %s", e)
        return []


S7SEED = {}
if set(S7_AGENTS) & set(AG):
    try:
        S7SEED = json.load(open(os.path.join(HERE, BCFG["ranking"]["seed_file"]))).get("wallets") or {}
    except Exception as e:
        print("S7 seed unreadable:", e)


def s7_ranking(labels=None):
    """Current S7 wallet ranking (seed + S7's own closes); see s7_rank.py."""
    if labels is None:
        labels = [w.get("label", w["address"][:6]) for w in (CW.wallets if CW else load_wallets())]
    cfg = {k: v for k, v in (BCFG.get("ranking") or {}).items() if k in s7_rank.DEFAULT}
    return s7_rank.rank(labels, S7SEED, A["wallet_own"], cfg)


def s7_on_buy(c, s):
    """S7: copy the buy only if the wallet is ranked; tier 1 -> S7_TOP (bigger size), tier 2 -> S7_NEXT (smaller size)."""
    rows = s7_ranking()
    row = next((r for r in rows if r["wallet"] == s["label"]), None)
    tier = row["tier"] if row else 0
    meta = {"source": "whale_monitor", "wallets": [s["label"]], "signal": "copy", "leader_buy_sol": round(s["value_sol"], 3), "tier": tier}
    if not tier:
        if enabled("S7_NEXT") or enabled("S7_TOP"):
            ag = "S7_NEXT" if "S7_NEXT" in AG else "S7_TOP"
            AST[ag]["signals"] += 1
            record(ag, c["mint"], c["symbol"], c["phase"], False, [f"wallet_not_copied({s['label']}: {(row or {}).get('why') or 'not ranked'})"], meta)
        return
    ag = S7_TIER_AGENT[tier]
    if ag in AG and enabled(ag):
        classic_enter(ag, c, meta)


def s6_confirm_due():
    """S6_CONFIRM: buy only if, confirm_seconds after the watched wallet's buy, the price is within [confirm_min_x, confirm_max_x]."""
    now = time.time()
    due = [q for q in CONFIRM_Q if q["due"] <= now]
    for q in due:
        CONFIRM_Q.remove(q)
        S, m = AG["S6_CONFIRM"], q["mint"]
        if not enabled("S6_CONFIRM") or m in A["positions"] or m in TRADED:
            continue
        c = build_cand(m, sources.ds_pairs([m]).get(m), "whale:" + q["label"])
        if not c or not valid_px(c.get("mark_sol")):
            record("S6_CONFIRM", m, q["symbol"], "?", False, ["confirm_no_price"], {"wallets": [q["label"]], "signal": "copy_confirmed"}); continue
        x = c["mark_sol"] / q["p0"]
        meta = {"source": "whale_monitor", "wallets": [q["label"]], "signal": "copy_confirmed", "confirm_x": round(x, 3), "leader_buy_sol": q["value_sol"]}
        if not S["confirm_min_x"] <= x <= S["confirm_max_x"]:
            AST["S6_CONFIRM"]["signals"] += 1
            record("S6_CONFIRM", m, c["symbol"], c["phase"], False, [f"not_confirmed({x:.2f}x_after_{S['confirm_seconds']}s)"], meta); continue
        classic_enter("S6_CONFIRM", c, meta)


def copy_worker(cw):
    wm = os.path.getmtime(WALLETS_F) if os.path.exists(WALLETS_F) else 0
    while not STOP.is_set():
        try:
            if CONFIRM_Q:
                s6_confirm_due()
        except Exception as e:
            log.exception("confirm error %s", e)
        try:
            m_ = os.path.getmtime(WALLETS_F) if os.path.exists(WALLETS_F) else 0
            if m_ != wm:
                wm = m_
                cw.wallets = load_wallets()
                log.info("watch list reloaded: %d wallets", len(cw.wallets))
            s = SIGQ.get(timeout=1)
        except queue.Empty:
            continue
        try:
            m = s["mint"]
            if s["type"] == "copy_buy":
                GLOBAL["leader_buys"] += 1
                LEAD_BUY[m][s["label"]] = time.time()
                want = [a for a in COPY_AGENTS if enabled(a)]     # V1/S5 host only COPY: unchanged behaviour
                if not want:
                    continue
                if not m.endswith("pump") and m not in COINS:
                    GLOBAL["leader_buy_not_pump"] += 1
                    continue
                c = build_cand(m, sources.ds_pairs([m]).get(m), "whale:" + s["label"])
                if not c:
                    GLOBAL["leader_buy_no_pair"] += 1
                    log.info("leader %s bought %s: no DEX pair data", s["label"], m)
                    continue
                log.info("leader %s bought %s (%s) [%s] ~%.2f SOL liq=$%.0f", s["label"], c["symbol"], m[:8], c["phase"], s["value_sol"], c["liq"])
                SEEN[m] = time.time()
                if "COPY" in want:
                    classic_enter("COPY", c, {"source": "whale_monitor", "wallets": [s["label"]], "signal": "copy", "leader_buy_sol": round(s["value_sol"], 3)})
                if "S6_CONFL" in want:
                    S = AG["S6_CONFL"]
                    labs = sorted(l for l, t in LEAD_BUY[m].items() if time.time() - t <= S["window_minutes"] * 60)
                    if len(labs) >= S["min_wallets"]:
                        classic_enter("S6_CONFL", c, {"source": "whale_monitor", "wallets": labs, "signal": "confluence", "leader_buy_sol": round(s["value_sol"], 3)})
                if "S6_COPY" in want and s["label"] not in AG["S6_COPY"].get("exclude_wallets", []):
                    classic_enter("S6_COPY", c, {"source": "whale_monitor", "wallets": [s["label"]], "signal": "copy", "leader_buy_sol": round(s["value_sol"], 3)})
                if any(a in want for a in S7_AGENTS):
                    s7_on_buy(c, s)
                if "S6_CONFIRM" in want and valid_px(c.get("mark_sol")) and m not in TRADED and not any(q["mint"] == m for q in CONFIRM_Q):
                    CONFIRM_Q.append({"due": time.time() + AG["S6_CONFIRM"]["confirm_seconds"], "mint": m, "label": s["label"], "symbol": c["symbol"],
                                      "p0": c["mark_sol"], "value_sol": round(s["value_sol"], 3)})
            elif s["type"] == "copy_sell":
                GLOBAL["leader_sells"] += 1
                LEAD_SELL[m][s["label"]] = time.time()
                p = A["positions"].get(m)
                if p and p["agent"] == "COPY" and s["label"] in p["wallets"] and AG["COPY"].get("exit_when_leader_sells"):
                    if not sell(m, 1, "whale_distribution", True):
                        p["pending_exit"] = "whale_distribution"
                elif (p and p["agent"].startswith(("S6_", "S7_")) and s["label"] in p["wallets"] and AG[p["agent"]].get("exit_when_leader_sells")
                      and not p.get("tp_done")):          # S6/S7: leader sold before our 2x -> out; after 2x the moonbag keeps its own exits
                    if not sell(m, 1, "leader_sold", True):
                        p["pending_exit"] = "leader_sold"
        except Exception as e:
            log.exception("copy signal error %s", e)


# ------------------------------------------------------------------ pipeline 3: pump.fun new/active lists -> M4 scan tier
LAST_EVAL = {}


def m4_evaluate(mint, pair, coin, source):
    """m4_momentum.py evaluate(), scan tier only (the tier that produced the profitable trades)."""
    S = AG["M4_SCAN"]
    EN, AR = S["entry"], S["antirug"]
    agent = "M4_SCAN"
    LAST_EVAL[mint] = time.time()
    if mint in TRADED or mint in A["positions"] or not pair:
        if not pair: GLOBAL["no_dex_pair_yet"] += 1
        return
    sym = (pair.get("baseToken") or {}).get("symbol") or (coin or {}).get("symbol") or mint[:6]
    created = (coin or {}).get("created_timestamp") or pair.get("pairCreatedAt")
    age = (time.time() * 1000 - created) / 60000 if created else None
    tx5 = (pair.get("txns") or {}).get("m5") or {}
    b5, s5 = tx5.get("buys", 0), tx5.get("sells", 0)
    n5 = b5 + s5
    vol5 = (pair.get("volume") or {}).get("m5") or 0
    pc = pair.get("priceChange") or {}
    m5, h1 = pc.get("m5") or 0, pc.get("h1") or 0
    mc = pair.get("marketCap") or pair.get("fdv") or (coin or {}).get("usd_market_cap") or 0
    ath = (coin or {}).get("ath_market_cap")
    graduated = bool((coin or {}).get("complete")) or pair.get("dexId") not in (None, "pumpfun")
    phase = "graduated" if graduated else "bonding"
    if n5 < 20 or (age is not None and age > EN["max_age_minutes"]):
        GLOBAL["m4_quiet_or_old_skipped"] += 1
        return
    AST[agent]["signals"] += 1
    det = {"tier": "scan", "age_min": age and round(age, 1), "vol5_usd": vol5, "buys5": b5, "sells5": s5, "m5": m5, "h1": h1, "mcap_usd": mc, "ath_usd": ath, "source": source}
    R = []
    if age is None: R.append("age_unknown")
    if mc < EN["min_mcap_usd"]: R.append(f"mcap_too_small(${mc:,.0f}<${EN['min_mcap_usd']:,})")
    if mc > EN["max_mcap_usd"]: R.append(f"mcap_too_big(${mc:,.0f})")
    if vol5 < EN["min_vol_5m_usd"]: R.append(f"low_5m_volume(${vol5:,.0f}<${EN['min_vol_5m_usd']:,})")
    if n5 < EN["min_trades_5m"]: R.append(f"few_5m_trades({n5}<{EN['min_trades_5m']})")
    if b5 < EN["min_buy_sell_ratio_5m"] * max(1, s5): R.append(f"buys_not_beating_sells({b5}/{s5}<{EN['min_buy_sell_ratio_5m']})")
    if m5 < EN["min_price_change_5m_pct"]: R.append(f"price_not_rising(5m {m5:+.0f}%)")
    if m5 > EN["max_price_change_5m_pct"]: R.append(f"already_vertical(5m +{m5:.0f}%)")
    if h1 > EN["max_price_change_1h_pct"]: R.append(f"already_vertical(1h +{h1:.0f}%)")
    if ath and mc and mc < ath * EN["min_mcap_vs_ath_pct"] / 100: R.append(f"crashed_from_ath({mc / ath * 100:.0f}%_of_ATH)")
    sold = [l for l, t in LEAD_SELL.get(mint, {}).items() if time.time() - t < 20 * 60]
    if sold: R.append("tracked_wallet_sold(" + ",".join(sold) + ")")
    if (coin or {}).get("is_banned"): R.append("pumpfun_banned")
    if R:
        record(agent, mint, sym, phase, False, R, det); return
    R = bot_gate(agent, mint)
    size = size_for(agent)
    if size <= 0: R.append("insufficient_balance")
    if R:
        record(agent, mint, sym, phase, False, R, det); return
    rep = sources.rc_report(mint, ttl=120)
    if not rep:
        record(agent, mint, sym, phase, False, ["no_rugcheck_report"], det); return
    if rep.get("mintAuthority") and AR["require_mint_authority_revoked"]: R.append("mint_authority_not_revoked")
    if rep.get("freezeAuthority") and AR["require_freeze_authority_revoked"]: R.append("freeze_authority_not_revoked")
    if rep.get("rugged"): R.append("rugcheck_marked_rugged")
    sn = rep.get("score_normalised")
    det["rc_score"] = sn
    if sn is not None and sn > AR["max_rugcheck_score"]: R.append(f"rugcheck_score({sn}>{AR['max_rugcheck_score']})")
    dangers = [r["name"] for r in rep.get("risks") or [] if r.get("level") == "danger"]
    if dangers and AR["reject_rugcheck_danger"]: R.append("rugcheck_danger:" + "|".join(dangers)[:90])
    t10, t1 = riskscore.holders(rep)
    if t10 is not None and t10 > AR["max_top10_pct"]: R.append(f"top10_holders({t10:.0f}%>{AR['max_top10_pct']}%)")
    if t1 is not None and t1 > AR["max_single_holder_pct"]: R.append(f"single_holder({t1:.0f}%>{AR['max_single_holder_pct']}%)")
    ins = rep.get("graphInsidersDetected") or 0
    if ins > AR["max_insiders"]: R.append(f"insider_network({ins})")
    phase = pumpfun.phase_of(rep, pair, coin)
    det["phase"] = phase
    liq = (pair.get("liquidity") or {}).get("usd") or rep.get("totalMarketLiquidity") or 0
    det["liq_usd"] = liq
    prog = None
    if phase == "bonding":
        prog = pumpfun.curve_progress_from_report(rep)
        det["curve_progress"] = prog
        if prog is not None and prog < EN["bonding_min_progress_pct"]: R.append(f"curve_progress_low({prog:.0f}%)")
        if prog is not None and prog > EN["bonding_max_progress_pct"]: R.append(f"curve_about_to_migrate({prog:.0f}%)")
    else:
        if liq < EN["graduated_min_liquidity_usd"]: R.append(f"liquidity_below_min({liq:.0f}<{EN['graduated_min_liquidity_usd']})")
        kind, locked = riskscore.lp_status(rep)
        if kind == "amm" and locked < AR["min_lp_locked_pct"]: R.append(f"lp_not_locked({locked:.0f}%)")
        if kind == "clmm_only": R.append("lp_pullable_clmm_only")
    sv = ((coin or {}).get("security_verdict") or {}).get("verdict")
    if sv and sv != "allow": R.append(f"pumpfun_security_verdict_{sv}")
    if R:
        record(agent, mint, sym, phase, False, R, det); return
    creator = (coin or {}).get("creator") or rep.get("creator")
    hit = _pump_cache.get(mint)
    if hit and time.time() - hit[0] < 120:
        pd = hit[1]
    else:
        pd = {"creator": creator, "dev_history": pumpfun.creator_history(creator) if creator else None, "launch": pumpfun.launch_analysis(mint, creator) if creator else {}}
        _pump_cache[mint] = (time.time(), pd)
    dh, la = pd.get("dev_history"), pd.get("launch") or {}
    if dh and dh["coins"] >= AR["serial_launcher_coins"] and dh["graduated"] == 0:
        R.append(f"serial_launcher({dh['coins']}{'+' if dh.get('capped_at_50') else ''}_coins_0_graduated)")
    if la.get("trades_fetched"):
        dev_net = (la.get("dev_bought_pct_supply") or 0) * (1 - min(100, la.get("dev_sold_pct_of_buys") or 0) / 100)
        if dev_net > AR["max_dev_hold_pct"]: R.append(f"dev_holds_too_much({dev_net:.1f}%)")
        if (la.get("dev_sold_pct_of_buys") or 0) >= AR["max_dev_sold_pct_of_buys"] and (la.get("dev_bought_pct_supply") or 0) > 0.5:
            R.append(f"dev_dumping(sold_{la['dev_sold_pct_of_buys']:.0f}%)")
        if (la.get("recent_unique_traders") or 0) < EN["min_unique_traders"]: R.append(f"few_unique_traders({la.get('recent_unique_traders')})")
        if la.get("reached_launch"):
            if (la.get("bundle_pct_supply") or 0) > AR["max_bundle_pct"]: R.append(f"bundled_launch({la['bundle_pct_supply']:.0f}%_supply)")
            if (la.get("launch_block_buyers") or 0) > AR["max_launch_block_buyers"]: R.append(f"sniper_cluster({la['launch_block_buyers']}_wallets_in_launch_block)")
    else:
        R.append("pumpfun_trade_history_unavailable")
    if R:
        record(agent, mint, sym, phase, False, R, det); return
    route = route_check(mint, size)
    if not route["buy_ok"]: R.append("no_buy_route")
    if not route["sell_ok"]: R.append("no_sell_route(honeypot_risk)")
    elif route["round_trip_loss_pct"] > EN["max_round_trip_loss_pct"]: R.append(f"exit_cost_too_high({route['round_trip_loss_pct']:.1f}%)")
    crit = {"min_lp_locked_pct": AR["min_lp_locked_pct"], "max_top10_pct": AR["max_top10_pct"], "max_single_pct": AR["max_single_holder_pct"],
            "max_round_trip_loss_pct": EN["max_round_trip_loss_pct"], "veto_clmm_only": True, "max_dev_hold_pct": AR["max_dev_hold_pct"],
            "max_bundle_pct": AR["max_bundle_pct"], "serial_launcher_coins": AR["serial_launcher_coins"]}
    score, vetoes, comp, unc = riskscore.pump_score(pair, rep, route if route["buy_ok"] else None, crit, dict(pd, phase=phase, coin=coin, progress=prog, age_min=age))
    det.update(rug_score=score, route={k: route.get(k) for k in ("round_trip_loss_pct", "route")})
    R += ["veto:" + v for v in vetoes if not any(v.split("(")[0] in r for r in R)]
    if score > EN["max_pump_rug_score"]: R.append(f"rug_score({score}>{EN['max_pump_rug_score']})")
    if R:
        record(agent, mint, sym, phase, False, R, det); return
    dec = ((rep or {}).get("token") or {}).get("decimals") or 6
    tokens = route["tokens_raw"] / 10 ** dec * (1 - LAT)
    ds_mark = mark_of(pair)
    if ds_mark and size / tokens > ds_mark * (1 + EN["max_fill_vs_mark_pct"] / 100):
        record(agent, mint, sym, phase, False, [f"price_ran_since_data(fill_{(size / tokens / ds_mark - 1) * 100:.0f}%_above_mark)"], det); return
    open_position(agent, mint, sym, phase, size, tokens, dec, liq, {"rug_score": score, "tier": "scan", "n5_last": None}, dict(det, signal="scan"))


def m4_loop():
    """m4_momentum.py discovery_loop (pump.fun new + active lists every 15 s, re-check a token at most every 75 s)."""
    while not STOP.is_set():
        t0 = time.time()
        try:
            if enabled("M4_SCAN"):
                update_sol()
                EN = AG["M4_SCAN"]["entry"]
                found = {}
                for tag, q in (("pf_new", "sort=created_timestamp&order=DESC"), ("pf_active", "sort=last_trade_timestamp&order=DESC")):
                    d = http.get(f"{pumpfun.PF}/coins?offset=0&limit=50&{q}&includeNsfw=false") or []
                    for c in d if isinstance(d, list) else []:
                        if not c.get("mint"): continue
                        COINS[c["mint"]] = c
                        if (time.time() * 1000 - (c.get("created_timestamp") or 0)) / 60000 <= EN["max_age_minutes"]:
                            found.setdefault(c["mint"], tag)
                due = [m for m in found if m not in TRADED and time.time() - LAST_EVAL.get(m, 0) >= EN["reevaluate_seconds"]]
                pairs = sources.ds_pairs(due) if due else {}
                for m in due:
                    if STOP.is_set(): break
                    m4_evaluate(m, pairs.get(m), COINS.get(m), found[m])
                CYCLE["m4"] = {"ts": time.time(), "found": len(found), "evaluated": len(due), "secs": round(time.time() - t0, 1)}
                log.info("cycle m4: %d new pump tokens, %d evaluated (%.0fs)", len(found), len(due), time.time() - t0)
        except Exception as e:
            log.exception("m4 loop error %s", e)
        STOP.wait(max(2, 15 - (time.time() - t0)))


# ------------------------------------------------------------------ pipeline 4: recently traded pump.fun coins -> S5 Revival
JUP_CHART = "https://datapi.jup.ag/v2/charts/{mint}?interval=1_MINUTE&to={to}&candles=1000&type=price"
LAST_CHECK = {}


def rev_features(tok, now, EN):
    cs = [x for x in tok["c"] if x[0] + 60 <= now]
    if len(cs) < 2:
        return None
    i, x = len(cs) - 1, cs[-1]
    peak = max(y[2] for y in cs[:-1])
    quiet = sum(y[5] for y in cs[:-1] if x[0] - EN["quiet_window_minutes"] * 60 <= y[0] < x[0])
    inflow = S5.net_inflow_sol(cs, i, 60) if x[6] == "bonding" else C.quote_reserve_sol(x[4], "graduated") - C.quote_reserve_sol(cs[-2][4], "graduated")
    return {"age_min": round((x[0] - tok["created"]) / 60, 1), "quiet_sol": round(quiet, 3), "wake_vol_sol": round(x[5], 3), "inflow_sol": round(inflow or 0, 3),
            "vs_peak": round(x[4] / peak, 3) if peak else None, "close": x[4], "phase": x[6], "candle_age_s": round(now - (x[0] + 60))}


def rev_reasons(f, EN):
    R = []
    if f["candle_age_s"] > 90: R.append("no_trade_last_minute")
    if f["age_min"] < EN["min_age_minutes"]: R.append(f"too_young({f['age_min']:.0f}m)")
    if f["quiet_sol"] > EN["quiet_max_volume_sol"]: R.append(f"not_quiet_before({f['quiet_sol']:.1f}_SOL_in_{EN['quiet_window_minutes']}m)")
    if f["wake_vol_sol"] < EN["wake_min_sol"]: R.append(f"wake_volume_low({f['wake_vol_sol']:.1f}<{EN['wake_min_sol']:g}_SOL)")
    if f["inflow_sol"] < EN["wake_min_sol"]: R.append(f"net_buying_low({f['inflow_sol']:.1f}<{EN['wake_min_sol']:g}_SOL)")
    if f["vs_peak"] is None or f["vs_peak"] > EN["max_price_vs_peak"]: R.append(f"not_far_below_peak({(f['vs_peak'] or 0) * 100:.0f}%_of_peak)")
    return R


def rev_evaluate(coin, now, SIG):
    """s5_quant.py evaluate() + enter()."""
    agent, EN = "REVIVAL", AG["REVIVAL"]["entry"]
    mint, sym = coin["mint"], coin.get("symbol") or coin["mint"][:6]
    LAST_CHECK[mint] = now
    d = http.get(JUP_CHART.format(mint=mint, to=int(now * 1000)))
    raw = (d or {}).get("candles") or []
    if not raw or not MKT["sol_usd"]:
        GLOBAL["no_candles"] += 1
        return
    tok = S5.jup_to_tok(raw, MKT["sol_usd"], (coin.get("created_timestamp") or 0) / 1000, bool(coin.get("complete")))
    f = rev_features(tok, now, EN)
    if not f:
        return
    fired = S5.fires_now(SIG, tok, now)
    phase = f["phase"]
    if not fired:
        if f["candle_age_s"] <= 90 and (f["wake_vol_sol"] >= EN["wake_min_sol"] / 2 or (f["quiet_sol"] <= EN["quiet_max_volume_sol"] and (f["vs_peak"] or 1) <= EN["max_price_vs_peak"])):
            AST[agent]["signals"] += 1
            record(agent, mint, sym, phase, False, rev_reasons(f, EN) or ["rule_not_met"], f)
        else:
            GLOBAL["revival_quiet_skipped"] += 1
        return
    AST[agent]["signals"] += 1
    R = bot_gate(agent, mint)
    size = size_for(agent)
    if size <= 0: R.append("insufficient_balance")
    if R:
        record(agent, mint, sym, phase, False, R, f); return
    qb = sources.jup_quote(WSOL, mint, size * 1e9, G["max_slippage_bps"])
    if not (qb and qb.get("outAmount") and int(qb["outAmount"]) > 0):
        record(agent, mint, sym, phase, False, ["no_buy_route"], f); return
    dec = 6
    tokens = int(qb["outAmount"]) / 10 ** dec * (1 - LAT)
    if sell_quote(mint, tokens, dec) is None:
        record(agent, mint, sym, phase, False, ["no_sell_route(honeypot_risk)"], f); return
    mark = mark_of(sources.ds_pairs([mint]).get(mint)) or f["close"]
    open_position(agent, mint, sym, phase, size, tokens, dec, None, {"entry_basis": mark}, dict(f, signal="revival"))


def revival_loop():
    EN = AG["REVIVAL"]["entry"]
    SIG = S5.make_revival(EN["min_age_minutes"], EN["quiet_window_minutes"], EN["quiet_max_volume_sol"], EN["wake_min_sol"], EN["max_price_vs_peak"])
    while not STOP.is_set():
        t0 = time.time()
        try:
            if enabled("REVIVAL"):
                update_sol()
                coins = {}
                for page in range(EN["scan_list_pages"]):
                    d = http.get(f"{pumpfun.PF}/coins?offset={page * 50}&limit=50&sort=last_trade_timestamp&order=DESC&includeNsfw=false") or []
                    for c in d if isinstance(d, list) else []:
                        if c.get("mint"): coins[c["mint"]] = c
                now = time.time()
                due = []
                for m, c in coins.items():
                    age = now - (c.get("created_timestamp") or now * 1000) / 1000
                    if age < EN["min_age_minutes"] * 60 or c.get("is_banned"):
                        continue
                    ath, mc = c.get("ath_market_cap"), c.get("usd_market_cap")
                    if ath and mc and mc > ath * (EN["max_price_vs_peak"] + 0.1):
                        continue
                    if now - LAST_CHECK.get(m, 0) < EN["recheck_seconds"]:
                        continue
                    due.append(c)
                if len(LAST_CHECK) > 20000:
                    for k in [k for k, v in LAST_CHECK.items() if now - v > 3600]: LAST_CHECK.pop(k, None)
                for c in due:
                    if STOP.is_set(): break
                    GLOBAL["revival_checked"] += 1
                    rev_evaluate(c, time.time(), SIG)
                CYCLE["revival"] = {"ts": time.time(), "found": len(coins), "evaluated": len(due), "secs": round(time.time() - t0, 1)}
                log.info("cycle revival: %d recently traded coins, %d checked (%.0fs)", len(coins), len(due), time.time() - t0)
        except Exception as e:
            log.exception("revival loop error %s", e)
        STOP.wait(max(2, AG["REVIVAL"]["entry"]["scan_interval_seconds"] - (time.time() - t0)))


# ------------------------------------------------------------------ monitor: every open position through its OWN sub-agent's exits
def monitor_loop():
    last_status, last_eq = 0, 0
    while not STOP.is_set():
        t0 = time.time()
        try:
            update_sol()
            held = list(A["positions"])
            dsp = sources.ds_pairs(held) if held else {}
            jp = sources.jup_prices(held) if held else {}
            solp = (jp.get(WSOL) or {}).get("usdPrice") or MKT["sol_usd"]
            for m in held:
                p = A["positions"].get(m)
                if not p:
                    continue
                try:
                    ag = p["agent"]
                    X = AG[ag]["exit"]
                    now = time.time()
                    pr = dsp.get(m)
                    if pr and (pr.get("liquidity") or {}).get("usd"):
                        p["last_liq"] = pr["liquidity"]["usd"]
                    if p.get("pending_exit"):
                        sell(m, 1, p["pending_exit"], True)
                        continue
                    if X["engine"] == "classic":       # pump_sentinel price: Jupiter price API, DexScreener SOL pair as fallback
                        mark = jp[m]["usdPrice"] / solp if (m in jp and solp and (jp[m] or {}).get("usdPrice")) else None
                        if mark is None and pr and (pr.get("quoteToken") or {}).get("address") == WSOL:
                            mark = float(pr.get("priceNative") or 0) or None
                        mark = mark if valid_px(mark) else None
                        acts = EXI.classic(p, X, now, mark, (REG.get("pc") or {}).get("h1"), AG[ag].get("regime") if AG[ag].get("use_regime_gate") else None)
                    elif X["engine"] == "m4":
                        mark = mark_of(pr)
                        n5 = None
                        if pr:
                            tx = (pr.get("txns") or {}).get("m5") or {}
                            n5 = tx.get("buys", 0) + tx.get("sells", 0)
                        acts = EXI.m4(p, X, now, mark if valid_px(mark) else None, n5)
                    else:
                        mark = mark_of(pr)
                        ph = "graduated" if (pr or {}).get("dexId") not in (None, "pumpfun") else "bonding"
                        acts = EXI.card(p, X, now, mark if valid_px(mark) else None, ph)
                    filled = True
                    for a in acts:
                        if a[0] == "final":
                            if not sell(m, 1, a[-1], True) and m in A["positions"]:
                                p["pending_exit"] = a[-1]
                            break
                        elif a[0] == "initials":
                            pull_initials(m)
                        elif a[0] == "partial":
                            if not sell(m, a[1], a[2], False):
                                filled = False
                                break       # not filled: the card state is not advanced, retried next tick
                    nxt = p.pop("engine_next", None)
                    if X["engine"] == "card" and m in A["positions"] and nxt and filled:
                        p["engine"] = nxt
                    if m in A["positions"] and now - p.get("value_ts", 0) > 15:
                        v = sell_quote(m, p["tokens"], p["decimals"])
                        p["value_ts"] = now
                        if v is not None: p["value_sol"] = v
                except Exception as e:
                    log.exception("position check error %s: %s", p.get("symbol"), e)
            eq = equity()
            GATE.roll(time.time(), eq)
            if time.time() - last_eq > 30:
                last_eq = time.time()
                dbx("INSERT INTO equity VALUES (?,?,?,?,?)", (RUN, time.time(), eq, A["balance"], len(A["positions"])))
            if time.time() - last_status > 60:
                last_status = time.time()
                log.info("STATUS %s: eq=%.4f open=%d closed=%d | bal=%.4f day=%+.2f%% on=%s", BOT, eq, len(A["positions"]), A["closed_n"], A["balance"],
                         GATE.day_pnl_pct(eq), ",".join(a for a in AG if TOG.on(a)) or "none")
                persist()
        except Exception as e:
            log.exception("monitor error %s", e)
        STOP.wait(max(0.5, 3 - (time.time() - t0)))


def state_loop():
    """Every 2 s, independent of the (network-bound) monitor loop: pick up switch changes, record them, and publish the
    state file, so the app sees a switch change within a few seconds even while positions are being priced."""
    last_tog = None
    while not STOP.is_set():
        try:
            tg = TOG.refresh()
            if tg != last_tog:
                if last_tog is not None:
                    log.info("SWITCHES changed (rev %s): %s", TOG.rev(), ", ".join(f"{k}={'on' if v else 'off'}" for k, v in tg.items()))
                dbx("INSERT INTO toggles VALUES (?,?,?,?)", (RUN, time.time(), TOG.rev(), json.dumps(tg)))
                last_tog = dict(tg)
            write_state(equity())
        except Exception as e:
            log.exception("state loop error %s", e)
        STOP.wait(2)


def write_state(eq):
    with LOCK:
        hs = []
        for p in A["positions"].values():
            h = {k: p.get(k) for k in ("agent", "mint", "symbol", "phase", "opened", "size_sol", "tokens", "tokens0", "decimals", "entry_mark", "last_mark", "peak_mark",
                                        "stop_mark", "stage", "moon_peak", "trail_pct", "tp_done", "recovered_sol", "fees", "wallets", "value_sol", "value_ts", "plan",
                                        "rug_score", "signal")}
            hs.append(h)
        halted = (GATE.day is not None and GATE.day_pnl_pct(eq) <= -RK.get("daily_stop_pct", 1e9))
        broke = None                                   # paper cash too small for the bot's own minimum trade size -> it can never enter again
        if RK.get("sizing") == "v1" and not hs:
            can = min(A["balance"] * RK["position_pct"] / 100, RK["max_position_sol"], A["balance"] - RK["reserve_sol"])
            if can < RK["min_position_sol"]:
                broke = (f"paper balance {A['balance']:.4f} SOL is too small: {RK['position_pct']:g}% = {max(can, 0):.4f} SOL "
                         f"< minimum trade {RK['min_position_sol']:g} SOL, so no new entries (needs a paper bankroll reset)")
        if RK.get("sizing") == "tier" and not hs and s7_rank.size_for_tier(1, A["balance"], RK) <= 0:
            broke = (f"paper balance {A['balance']:.4f} SOL is too small for the minimum trade {RK['min_position_sol']:g} SOL "
                     f"(keeps {RK['reserve_sol']:g} SOL), so no new entries (needs a paper bankroll reset)")
        tg = TOG.refresh()
        agents = {}
        for a, S in AG.items():
            agents[a] = {"title": S["title"], "origin": S["origin"], "kind": S["kind"], "on": bool(tg.get(a)), "default_on": bool(BCFG["agents"][a]),
                         "plan": EXI.plan(S["exit"]), "counters": dict(AST[a]), "last": LASTD.get(a), "open": sum(1 for p in A["positions"].values() if p["agent"] == a)}
        st = {"run": RUN, "ts": time.time(), "pid": os.getpid(), "bot": BOT, "title": BCFG["general"]["title"], "equity": eq, "balance": A["balance"],
              "start": A["start"], "open": len(hs), "closed_n": A["closed_n"], "pnl": A["pnl"], "holdings": hs, "sol_usd": MKT["sol_usd"],
              "regime": {"ok": REG["ok"], "why": REG["why"], "pc": REG.get("pc")}, "day_pnl_pct": GATE.day_pnl_pct(eq),
              "halted": (f"daily auto-stop: day at {GATE.day_pnl_pct(eq):.1f}% (limit -{RK['daily_stop_pct']:g}%), resumes 00:00 Dubai" if halted else broke),
              "switches": tg, "switch_rev": TOG.rev(), "agents": agents, "risk": RK, "counters": dict(GLOBAL.most_common(40)), "cycles": CYCLE,
              "wallets_watched": len(CW.wallets) if CW else 0}
        if set(S7_AGENTS) & set(AG):
            st["wallet_rank"] = [dict(r, net=round(r["net"], 4), pf=round(r["pf"], 2), own_net=round(r["own_net"], 4),
                                      win_rate=r["win_rate"] and round(r["win_rate"])) for r in s7_ranking()]
    tmp = f"{STATE_F}.{threading.get_ident()}.tmp"
    json.dump(st, open(tmp, "w"), default=str)
    os.replace(tmp, STATE_F)


# ------------------------------------------------------------------ main
log.info("%s multi-agent PAPER run %s (pid %d); resumed=%s balance=%.4f open=%d; agents=%s; switches=%s", BOT, RUN, os.getpid(),
         bool(A["positions"] or A["closed_n"]), A["balance"], len(A["positions"]), list(AG), TOG.refresh())
if GATE.day is None:
    GATE.day_start(time.time(), equity())
update_sol()
CW = None
if set(COPY_AGENTS) & set(AG):
    shim = {"rpc": {"url": V1CFG["rpc"]["url"]}, "copytrade": {"wallets": load_wallets(), "poll_seconds": V1CFG["copytrade"]["poll_seconds"],
            "min_followed_buy_sol": V1CFG["copytrade"]["min_followed_buy_sol"]}}
    CW = CopyWatcher(shim, SIGQ.put, GLOBAL)
    CW.start()
    threading.Thread(target=copy_worker, args=(CW,), daemon=True).start()
loops = [monitor_loop, state_loop]
if {"V1_SNIPE", "V1_SCANNER", "C_GRAD", "LS", "S6_CGRAD"} & set(AG): loops.append(dex_loop)
if "M4_SCAN" in AG: loops.append(m4_loop)
if "REVIVAL" in AG: loops.append(revival_loop)
for f in loops:
    threading.Thread(target=f, daemon=True, name=f.__name__).start()
TERM = threading.Event()
signal.signal(signal.SIGTERM, lambda *a: TERM.set())
try:
    while not TERM.is_set():
        time.sleep(1)
except KeyboardInterrupt:
    pass
STOP.set()
if CW: CW.stop.set()
time.sleep(2)
persist()
log.info("%s stopped; %d open positions kept for the next start", BOT, len(A["positions"]))
