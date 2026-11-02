#!/usr/bin/env python3
"""PUMP.FUN-ONLY SENTINEL harness (bonding-curve and graduated buckets tracked separately).
SENTINEL strategy-validation harness, PAPER ONLY (no wallet, no keys, no transactions).
Runs the ORIGINAL spec rules, the REVISED rules (with the launch-sniper tier) and the user's V1 sniper rules
side by side on the same live data, each in its own simulated 1-SOL account, against hold-SOL and no-trade.

  python3 sentinel.py --minutes 35
"""
import signal, argparse, collections, csv, json, logging, math, os, queue, sqlite3, sys, threading, time, tomllib
from bot import sources, rugfilter, scoring, riskscore, http, pumpfun
from bot.copytrade import CopyWatcher

ap = argparse.ArgumentParser()
ap.add_argument("--config", default="pump_sentinel.toml")
ap.add_argument("--v1-config", default="config.toml")
ap.add_argument("--minutes", type=float, default=35)
ap.add_argument("--resume", action="store_true", help="continue the saved run (accounts, open positions, history) if it hasn't expired")
ap.add_argument("--keep-open-on-exit", action="store_true",
                help="on SIGTERM save open positions for the next start instead of selling them (24/7 runner handover)")
args = ap.parse_args()
CFG = tomllib.load(open(args.config, "rb"))
V1CFG = tomllib.load(open(args.v1_config, "rb"))
G = CFG["general"]
assert G["paper"] is True
os.makedirs("data", exist_ok=True)
RUNF, ACCTF = "data/pump_run.json", "data/pump_accounts.json"
RESUME = None
if args.resume and os.path.exists(RUNF):
    try:
        _r = json.load(open(RUNF))
        if time.time() - _r["t0"] < args.minutes * 60:
            RESUME = _r
    except Exception:
        RESUME = None
RUN = RESUME["run"] if RESUME else time.strftime("P%Y%m%d-%H%M%S")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-8s %(message)s", datefmt="%H:%M:%S",
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(f"data/pump_sentinel_{RUN}.log")])
log = logging.getLogger("sentinel")
LOCK = threading.RLock()
STOP = threading.Event()
WSOL = sources.WSOL
LAT = G["latency_slippage_bps"] / 1e4

db = sqlite3.connect("data/pump_sentinel.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS decisions (run TEXT, ts REAL, account TEXT, strategy TEXT, phase TEXT, mint TEXT, symbol TEXT,
  approved INTEGER, reasons TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS trades (run TEXT, account TEXT, strategy TEXT, phase TEXT, mint TEXT, symbol TEXT, opened REAL, closed REAL,
  size_sol REAL, sol_out REAL, fees_sol REAL, slippage_sol REAL, pnl_sol REAL, pnl_pct REAL, cost_pct REAL, exit_reason TEXT, record TEXT);
CREATE TABLE IF NOT EXISTS scans (run TEXT, ts REAL, mint TEXT, symbol TEXT, source TEXT, phase TEXT, pump_evidence TEXT, liq REAL, rug_score INTEGER,
  vetoes TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS regime (run TEXT, ts REAL, sol_usd REAL, m5 REAL, h1 REAL, h6 REAL, h24 REAL, ok INTEGER, why TEXT);
""")
CSV = "data/pump_trades.csv"
CSV_COLS = ["run", "account", "strategy", "phase", "pool_fee_pct", "symbol", "mint", "opened", "closed", "hold_s", "size_sol", "sol_out", "fees_sol",
            "slippage_sol", "pnl_sol", "pnl_pct", "cost_pct", "exit_reason", "rug_score", "wallets", "discovered_by"]
if not os.path.exists(CSV):
    csv.writer(open(CSV, "w", newline="")).writerow(CSV_COLS)


def dbx(sql, params):
    with LOCK:
        db.execute(sql, params)
        db.commit()


GLOBAL = collections.Counter()
REG = {"ok": True, "why": "init", "sol_usd": None}


# ------------------------------------------------------------ regime (Strategy F)
def update_regime():
    d = http.get("https://api.dexscreener.com/latest/dex/pairs/solana/58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2")
    p = ((d or {}).get("pairs") or [None])[0] or (d or {}).get("pair")
    if not p:
        REG.update(ok=False, why="regime_data_unavailable")
        return
    pc = p.get("priceChange") or {}
    r = CFG["regime"]
    why = []
    if (pc.get("h1") or 0) < r["sol_h1_min_pct"]: why.append(f"SOL_h1={pc.get('h1')}%")
    if (pc.get("h6") or 0) < r["sol_h6_min_pct"]: why.append(f"SOL_h6={pc.get('h6')}%")
    if (pc.get("h24") or 0) < r["sol_h24_min_pct"]: why.append(f"SOL_h24={pc.get('h24')}%")
    REG.update(ok=not why, why=",".join(why) or "ok", sol_usd=float(p.get("priceUsd") or 0), pc=pc, ts=time.time())
    dbx("INSERT INTO regime VALUES (?,?,?,?,?,?,?,?,?)", (RUN, time.time(), REG["sol_usd"], pc.get("m5"), pc.get("h1"),
                                                           pc.get("h6"), pc.get("h24"), int(REG["ok"]), REG["why"]))


# ------------------------------------------------------------ route check (Jupiter quotes, real)
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
            back = int(qs["outAmount"]) / 1e9
            r.update(sell_ok=True, sell_impact_pct=float(qs.get("priceImpactPct") or 0) * 100,
                     round_trip_loss_pct=(1 - back / size_sol) * 100)
    _route_cache[key] = r
    return r


def valid_px(x):
    """A usable price: a finite number above zero (DexScreener can return 0/None, e.g. for pairs quoted in another pump token)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def fees_of(strat):
    f = CFG["fees"][CFG["strategy"][strat]["fees"]]
    return f["base_fee_sol"] + f["priority_fee_sol"] + f["jito_tip_sol"]


# ------------------------------------------------------------ accounts
class Account:
    def __init__(self, name, a):
        self.name, self.a = name, a
        self.balance = self.start = G["start_balance_sol"]
        self.positions, self.closed, self.traded = {}, [], set()
        self.peak = self.day0 = self.week0 = self.start
        self.halted = None
        self.stats = collections.defaultdict(collections.Counter)
        self.rejects = collections.defaultdict(collections.Counter)
        self.cost_samples = collections.defaultdict(list)

    def equity(self):
        return self.balance + sum(p["tokens"] * p["last_mark"] + G["ata_rent_sol"] for p in self.positions.values())

    def breaker(self):
        if "daily_loss_pct" not in self.a:
            return None
        eq = self.equity()
        self.peak = max(self.peak, eq)
        if (self.day0 - eq) / self.day0 * 100 >= self.a["daily_loss_pct"]: self.halted = self.halted or "daily_loss_limit"
        if (self.week0 - eq) / self.week0 * 100 >= self.a["weekly_loss_pct"]: self.halted = self.halted or "weekly_loss_limit"
        if (self.peak - eq) / self.peak * 100 >= self.a["max_drawdown_pct"]: self.halted = self.halted or "drawdown_breaker"
        return self.halted


ACCTS = {n: Account(n, a) for n, a in CFG["accounts"].items()}


def persist_state():
    """Save accounts + open positions so a restart (--resume) continues the same paper run."""
    with LOCK:
        d = {"run": RUN, "ts": time.time(), "global": dict(GLOBAL), "accounts": {}}
        for n, a in ACCTS.items():
            d["accounts"][n] = {"balance": a.balance, "start": a.start, "positions": a.positions, "closed": a.closed, "traded": sorted(a.traded),
                                "peak": a.peak, "day0": a.day0, "week0": a.week0, "halted": a.halted,
                                "stats": {k: dict(v) for k, v in a.stats.items()}, "rejects": {k: dict(v) for k, v in a.rejects.items()},
                                "cost_samples": dict(a.cost_samples)}
        tmp = ACCTF + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, default=str)
        os.replace(tmp, ACCTF)


if RESUME and os.path.exists(ACCTF):
    try:
        _s = json.load(open(ACCTF))
        if _s.get("run") == RUN:
            GLOBAL.update(_s.get("global") or {})
            for n, sa in (_s.get("accounts") or {}).items():
                a = ACCTS.get(n)
                if not a:
                    continue
                a.balance, a.start, a.positions, a.closed = sa["balance"], sa["start"], sa["positions"], sa["closed"]
                a.traded, a.peak, a.day0, a.week0, a.halted = set(sa["traded"]), sa["peak"], sa["day0"], sa["week0"], sa["halted"]
                for p in a.positions.values(): p["last_ok"] = time.time()   # the handover gap is not "price data lost"
                for k, v in (sa.get("stats") or {}).items(): a.stats[k].update(v)
                for k, v in (sa.get("rejects") or {}).items(): a.rejects[k].update(v)
                for k, v in (sa.get("cost_samples") or {}).items(): a.cost_samples[k] = list(v)
    except Exception as e:
        print("could not restore accounts:", repr(e))


def exit_rules(acct, strat):
    if strat == "LS":
        return dict(CFG["strategy"]["LS"], **{k: v for k, v in acct.a["ls"].items() if k != "crit"})
    return CFG["strategy"][strat]


def size_for(acct, strat, cost_pct):
    if strat == "V1":
        return min(acct.balance * acct.a["position_pct"] / 100, acct.a["max_position_sol"], acct.balance - 0.05)
    if strat == "LS":
        return min(acct.a["ls"]["position_sol"], acct.balance - 0.02)
    sl = CFG["strategy"][strat]["stop_loss_pct"]
    eq = acct.equity()
    s = eq * min(acct.a["max_position_pct"] / 100, acct.a["risk_per_trade_pct"] / (sl + cost_pct))
    return max(0.0, min(s, acct.balance - 0.02))


def record_decision(acct, strat, cand, approved, reasons, detail):
    strat = strat if ":" in strat else f"{strat}:{cand.get('phase')}"
    st = acct.stats[strat]
    if approved:
        st["approved"] += 1
    else:
        st["rejected"] += 1
        for r in reasons:
            k = r.split("(")[0]
            if k.startswith("F_regime_cash"): k = "F_regime_cash"
            if k.startswith("v1_filter:rugcheck_danger"): k = "v1_filter:rugcheck_danger"
            acct.rejects[strat][k[:60]] += 1
    dbx("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)", (RUN, time.time(), acct.name, strat.split(":")[0], cand.get("phase"), cand["mint"], cand["symbol"],
                                                             int(approved), ";".join(reasons), json.dumps(detail, default=str)))


def decide_and_enter(acct, strat, cand, meta):
    """Deterministic approval pipeline. Returns position or None."""
    with LOCK:
        key = f"{strat}:{cand['phase']}"
        acct.stats[key]["signals"] += 1
        pair, report = cand["pair"], cand["report"]
        reasons, checks = [], []
        is_ls, is_v1 = strat == "LS", strat == "V1"
        R = exit_rules(acct, strat)
        # 1. Strategy F regime gate
        if acct.a.get("use_regime_gate") and strat in ("A", "B", "C"):
            if not REG["ok"]: reasons.append("F_regime_cash:" + REG["why"])
            else: checks.append("regime_ok")
        # 2. risk management
        br = acct.breaker()
        if br: reasons.append("risk_breaker:" + br)
        if cand["mint"] in acct.traded or cand["mint"] in acct.positions: reasons.append("already_traded(no_averaging)")
        if len(acct.positions) >= acct.a["max_concurrent"]: reasons.append("max_concurrent_positions")
        if is_ls:
            lsp = [p for p in acct.positions.values() if p["strategy"] == "LS"]
            if len(lsp) >= acct.a["ls"]["max_concurrent"]: reasons.append("ls_slot_full")
            if sum(p["size_sol"] for p in lsp) + acct.a["ls"]["position_sol"] > acct.start * acct.a["ls"]["tier_capital_pct"] / 100:
                reasons.append("ls_tier_capital_cap")
        # 3. liquidity + freshness
        liq = (pair.get("liquidity") or {}).get("usd") or 0
        if is_ls: min_liq = acct.a["ls"]["min_liquidity_usd"]
        elif is_v1: min_liq = V1CFG["filter"]["min_liquidity_usd"]
        elif cand["phase"] == "bonding": min_liq = R["min_liquidity_bonding_usd"]
        else: min_liq = R["min_liquidity_usd"]
        if liq < min_liq: reasons.append(f"liquidity_below_min({liq:.0f}<{min_liq})")
        if time.time() - cand["pair_ts"] > 90: reasons.append("stale_market_data")
        if report is None and not is_v1: reasons.append("security_data_unavailable")
        if reasons:
            record_decision(acct, strat, cand, False, reasons, {"stage": "pre"})
            return None
        checks += ["risk_limits_ok", f"liquidity_ok(${liq:,.0f})", "data_fresh"]
        # 4. security
        if is_v1:
            ok, rr_, info = rugfilter.check(pair, report, V1CFG)
            if not ok:
                record_decision(acct, strat, cand, False, ["v1_filter:" + r for r in rr_], info)
                return None
            checks.append("v1_rug_filter_pass")
            score, vetoes, comp = None, [], info
        crit = acct.a["ls"]["crit"] if is_ls else acct.a.get("crit")
        max_score = acct.a["ls"]["max_rug_score"] if is_ls else acct.a.get("max_rug_score")
        if not is_v1:
            pre, pv, _, _ = riskscore.score(pair, report, None, crit, cand["phase"])
            if pv or pre - 20 > max_score:   # cheap pre-check before spending Jupiter quotes
                reasons = ["veto:" + v for v in pv] + ([f"rug_score({pre - 20}+>{max_score})"] if pre - 20 > max_score else [])
                record_decision(acct, strat, cand, False, reasons, {"stage": "security_pre", "score_wo_route": pre - 20})
                return None
        # 5. route + size
        fees_rt = 2 * fees_of(strat)
        prov = size_for(acct, strat, 4.0)
        if prov <= 0:
            record_decision(acct, strat, cand, False, ["insufficient_balance"], {})
            return None
        route = route_check(cand["mint"], prov)
        pump = pump_data(cand) if not is_v1 else None
        if not route["buy_ok"]: reasons.append("no_buy_route")
        if not is_v1:
            score, vetoes, comp, unc = riskscore.pump_score(pair, report, route, crit, pump)
            reasons += ["veto:" + v for v in vetoes]
            if score > max_score: reasons.append(f"rug_score({score}>{max_score})")
            else: checks.append(f"rug_score_{score}({riskscore.band(score)})")
        elif not route.get("sell_ok"):
            reasons.append("no_sell_route")
        if route.get("sell_ok"):
            cost_pct = max(0.0, route["round_trip_loss_pct"]) + 2 * LAT * 100 + fees_rt / prov * 100
            checks.append("sell_route_verified")
        else:
            cost_pct = None
            if "veto:no_sell_route(honeypot_risk)" not in reasons and "no_sell_route" not in reasons:
                reasons.append("no_sell_route")
        size = size_for(acct, strat, cost_pct or 4.0)
        min_pos = acct.a["min_position_sol"] if not is_ls else 0.01
        if size < min_pos: reasons.append(f"size_below_min({size:.4f})")
        # 6. R:R after costs (A/B/C only)
        rr = None
        if strat in ("A", "B", "C") and cost_pct is not None:
            rr = (R["take_profit_pct"] - cost_pct) / (R["stop_loss_pct"] + cost_pct)
            if rr < acct.a["min_rr"]: reasons.append(f"rr_after_costs({rr:.2f}<{acct.a['min_rr']})")
            else: checks.append(f"rr_{rr:.2f}")
        detail = {"rug_score": score, "components": comp, "route": route, "cost_pct": cost_pct, "rr": rr, "size": size,
                  "regime": REG.get("why"), **meta}
        if reasons:
            record_decision(acct, strat, cand, False, reasons, detail)
            return None
        # 7. fill (re-quote at final size)
        q = route if abs(size - prov) < 1e-4 else route_check(cand["mint"], size)
        if not q.get("buy_ok"):
            record_decision(acct, strat, cand, False, ["requote_failed"], detail)
            return None
        dec = ((report or {}).get("token") or {}).get("decimals")
        if dec is None:
            record_decision(acct, strat, cand, False, ["no_decimals"], detail)
            return None
        tokens = q["tokens_raw"] / 10 ** dec * (1 - LAT)
        # cost basis = the real Jupiter fill price (SOL per token, incl. latency slippage). The DEX mark can be 0/None or
        # in another quote token (e.g. DOVE was quoted in PEACE with no USD price -> mark 0 -> monitor crash on 2026-11-02).
        fill = size / tokens if tokens > 0 else None
        ds_mark = cand["mark_sol"] if valid_px(cand.get("mark_sol")) else None
        if not valid_px(fill):
            record_decision(acct, strat, cand, False, [f"invalid_entry_price(fill={fill},mark={cand.get('mark_sol')})"], detail)
            return None
        mark = fill
        fee = fees_of(strat)
        acct.balance -= size + fee + G["ata_rent_sol"]
        pos = dict(account=acct.name, strategy=strat, phase=cand["phase"], pump_evidence=cand["pump_evidence"],
                   pool_fee_pct=pumpfun.pool_fee_pct(cand["phase"], cand.get("mcap_sol")), progress=cand.get("progress"), mint=cand["mint"], symbol=cand["symbol"], decimals=dec, opened=time.time(),
                   size_sol=size, tokens=tokens, entry_mark=mark, peak_mark=mark, last_mark=mark, last_ok=time.time(),
                   entry_liq=liq, last_liq=liq, fees=fee, entry_slip=(size - tokens * ds_mark) if ds_mark else 0.0, cost_pct_est=cost_pct, dex_entry_mark=ds_mark,
                   rug_score=score, checks=checks, rr=rr, wallets=meta.get("wallets", []), discovered_by=meta.get("source"),
                   wallet_evidence=meta.get("evidence"), size_reason=sizing_reason(acct, strat, size, cost_pct),
                   risk_accepted_sol=size * ((R["stop_loss_pct"] + (cost_pct or 0)) / 100),
                   exit_plan={k: R.get(k) for k in ("take_profit_pct", "stop_loss_pct", "breakeven_at_pct", "trailing_activate_pct",
                                                    "trailing_stop_pct", "max_hold_minutes", "liq_drop_exit_pct")})
        acct.positions[cand["mint"]] = pos
        acct.traded.add(cand["mint"])
        acct.stats[key]["entered"] += 1
        record_decision(acct, strat, cand, True, [], detail)
        log.info("ENTER [%s] %-9s %-3s %-10s size=%.4f SOL rug=%s cost~%.1f%% rr=%s liq=$%.0f via=%s wallets=%s",
                 cand["phase"], acct.name, strat, cand["symbol"], size, score, cost_pct or -1, rr and round(rr, 2), liq, meta.get("source"), pos["wallets"])
        return pos


def sizing_reason(acct, strat, size, cost):
    if strat == "V1": return f"V1: {acct.a['position_pct']}% of cash capped at {acct.a['max_position_sol']} SOL"
    if strat == "LS": return f"LS tier fixed {acct.a['ls']['position_sol']} SOL (tier cap {acct.a['ls']['tier_capital_pct']}% of start)"
    return (f"min({acct.a['max_position_pct']}% equity cap, {acct.a['risk_per_trade_pct']}% equity risk / "
            f"(SL {CFG['strategy'][strat]['stop_loss_pct']}% + cost {cost:.1f}%)) = {size:.4f} SOL")


def close(acct, mint, reason):
    with LOCK:
        pos = acct.positions.get(mint)
        if not pos:
            return
        raw = int(pos["tokens"] * 10 ** pos["decimals"])
        q = sources.jup_quote(mint, WSOL, raw, G["max_slippage_bps"])
        if q and q.get("outAmount"):
            sol_out, src = int(q["outAmount"]) / 1e9 * (1 - LAT), "jupiter_quote"
        else:
            pos["exit_fail"] = pos.get("exit_fail", 0) + 1
            if pos["exit_fail"] < 10 and reason != "end_of_run":
                return
            sol_out, src = 0.0, "NO_ROUTE_total_loss"
        fee = fees_of(pos["strategy"])
        fees = pos["fees"] + fee
        exit_slip = pos["tokens"] * pos["last_mark"] - sol_out
        pnl = sol_out - pos["size_sol"] - fees
        acct.balance += sol_out - fee + G["ata_rent_sol"]
        del acct.positions[mint]
        cost_pct = (fees + pos["entry_slip"] + exit_slip) / pos["size_sol"] * 100
        pkey = f"{pos['strategy']}:{pos['phase']}"
        acct.cost_samples[pkey].append(((fees + max(0, pos["entry_slip"]) + max(0, exit_slip)) / pos["size_sol"] * 100))
        rec = dict(pos, closed=time.time(), sol_out=sol_out, exit_reason=reason, exit_fill=src, pnl_sol=pnl,
                   pnl_pct=pnl / pos["size_sol"] * 100, exit_mark=pos["last_mark"])
        acct.closed.append(rec)
        acct.stats[pkey]["closed"] += 1
        db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (RUN, acct.name, pos["strategy"], pos["phase"], mint, pos["symbol"], pos["opened"], rec["closed"], pos["size_sol"], sol_out,
                    fees, pos["entry_slip"] + exit_slip, pnl, rec["pnl_pct"], cost_pct, reason, json.dumps(rec, default=str)))
        db.commit()
        csv.writer(open(CSV, "a", newline="")).writerow(
            [RUN, acct.name, pos["strategy"], pos["phase"], pos["pool_fee_pct"], pos["symbol"], mint, round(pos["opened"]), round(rec["closed"]),
             round(rec["closed"] - pos["opened"]), round(pos["size_sol"], 5), round(sol_out, 6), round(fees, 6),
             round(pos["entry_slip"] + exit_slip, 6), round(pnl, 6), round(rec["pnl_pct"], 2), round(cost_pct, 2), reason,
             pos["rug_score"], "|".join(pos["wallets"]), pos["discovered_by"]])
        log.info("EXIT  [%s] %-9s %-3s %-10s %s pnl=%+.5f SOL (%+.1f%%) hold=%ds fill=%s bal=%.4f", pos["phase"], acct.name, pos["strategy"],
                 pos["symbol"], reason, pnl, rec["pnl_pct"], rec["closed"] - pos["opened"], src, acct.balance)


# ------------------------------------------------------------ candidates & signals (pump.fun only)
COINS = {}      # mint -> pump.fun coin dict (from list endpoints)
_pump_cache = {}


def build_cand(mint, pair, source, coin=None):
    coin = coin or COINS.get(mint)
    if not pair and not coin:
        return None
    if pair and not pair.get("liquidity"):
        rep = sources.rc_report(mint)
        pair["liquidity"] = {"usd": (rep or {}).get("totalMarketLiquidity") or 0, "_from": "rugcheck"}
    if not pair:   # pump.fun knows it but DexScreener does not yet: build a minimal pair from pump.fun data
        return None
    tx = (pair.get("txns") or {}).get("m5") or {}
    if tx.get("buys", 0) < G["min_activity_buys_5m"] and not source.startswith("whale"):
        GLOBAL["skipped_inactive"] += 1
        return None
    liq = pair["liquidity"]["usd"] or 0
    report = sources.rc_report(mint) if liq >= G["min_liq_to_fetch_report"] or source.startswith("whale") else None
    ev = pumpfun.is_pump(mint, report, pair, coin)
    if not ev:
        GLOBAL["not_pump_token"] += 1
        return None
    phase = pumpfun.phase_of(report, pair, coin)
    native = float(pair.get("priceNative") or 0)
    if pair.get("quoteToken", {}).get("address") != WSOL:
        solp = REG.get("sol_usd")
        native = float(pair.get("priceUsd") or 0) / solp if solp else 0
    sc, parts = scoring.score(pair, None)
    mcap_sol = (pair.get("marketCap") or pair.get("fdv") or 0) / REG["sol_usd"] if REG.get("sol_usd") else None
    return dict(mint=mint, symbol=(pair.get("baseToken") or {}).get("symbol", "?"), pair=pair, pair_ts=time.time(),
                report=report, mark_sol=native, liq=liq, score=sc, parts=parts, age=scoring.age_minutes(pair), source=source,
                coin=coin, phase=phase, pump_evidence=ev, progress=pumpfun.curve_progress_from_report(report) if phase == "bonding" else None,
                mcap_sol=mcap_sol)


def pump_data(c):
    """dev history + launch analysis (pump.fun API), cached 5 min."""
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


def sig_C(c):
    S, p, pr = CFG["strategy"]["C"], c["parts"], c["pair"]
    if c["phase"] == "bonding":
        prog = c.get("progress") or 0
        return ((c["age"] or 0) >= S["bonding_min_age_minutes"] and prog >= S["bonding_min_progress_pct"] and
                c["liq"] >= S["min_liquidity_bonding_usd"] and p["b5"] >= S["bonding_min_buys_5m"] and
                p["b5"] >= S["bonding_min_buy_sell_ratio"] * max(1, p["s5"]) and p["m5"] > 0 and p["h1"] > 0)
    tx = pr.get("txns") or {}
    b1, s1 = (tx.get("h1") or {}).get("buys", 0), (tx.get("h1") or {}).get("sells", 0)
    vol = (pr.get("volume") or {}).get("h1") or 0
    return (c["liq"] >= S["min_liquidity_usd"] and (c["age"] or 0) >= S["min_age_minutes"] and vol / max(1, c["liq"]) >= S["min_vol_liq_h1"]
            and b1 >= S["min_buy_sell_ratio_h1"] * max(1, s1) and p["m5"] > 0 and 0 < p["h1"] < S["max_h1_pct"]
            and p["b5"] > p["s5"] and c["score"] >= S["min_score"])


def sig_LS(c, ls):
    p, prog = c["parts"], c.get("progress")
    return (c["phase"] == "bonding" and c["age"] is not None and c["age"] <= ls["max_age_minutes"] and prog is not None
            and ls["min_progress_pct"] <= prog <= ls["max_progress_pct"]
            and p["b5"] >= ls["min_buys_5m"] and p["b5"] >= ls["min_buy_sell_ratio"] * max(1, p["s5"]))


def sig_V1(c):
    s, p = V1CFG["strategy"], c["parts"]
    if c["age"] is not None and c["age"] <= s["snipe_max_age_minutes"] and p["b5"] >= s["snipe_min_buys_5m"] \
            and p["b5"] >= s["snipe_min_buy_sell_ratio"] * max(1, p["s5"]):
        return "snipe"
    if c["score"] >= s["scanner_min_score"] and p["m5"] > 0 and p["b5"] > p["s5"]:
        return "scanner"
    return None


SEEN = {}


def on_token(c):
    GLOBAL["pump_tokens_scanned"] += 1
    GLOBAL[f"phase:{c['phase']}"] += 1
    GLOBAL[f"pump_evidence:{c['pump_evidence']}"] += 1
    if c["report"] is not None:
        GLOBAL["security_reports"] += 1
        s0, v0, comp, unc = riskscore.score(c["pair"], c["report"], None, CFG["accounts"]["ORIGINAL"]["crit"], c["phase"])
        GLOBAL[f"generic_score_band_wo_route:{c['phase']}:{riskscore.band(max(0, s0 - 20))}"] += 1
        if v0: GLOBAL[f"critical_veto_generic:{c['phase']}"] += 1
        dbx("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?,?)", (RUN, time.time(), c["mint"], c["symbol"], c["source"], c["phase"],
                                                                 c["pump_evidence"], c["liq"], s0 - 20, ";".join(v0), json.dumps(comp, default=str)))
    else:
        GLOBAL["below_report_liquidity"] += 1
    meta = {"source": c["source"]}
    for name, acct in ACCTS.items():
        if "C" in acct.a["strategies"] and sig_C(c):
            decide_and_enter(acct, "C", c, dict(meta, signal="momentum_confirmation"))
        if "LS" in acct.a["strategies"] and sig_LS(c, acct.a["ls"]):
            decide_and_enter(acct, "LS", c, dict(meta, signal="new_launch"))
        if "V1" in acct.a["strategies"]:
            v = sig_V1(c)
            if v:
                decide_and_enter(acct, "V1", c, dict(meta, signal=v))


def discovery_loop():
    while not STOP.is_set():
        t0 = time.time()
        try:
            update_regime()
            found = {}
            pf = pumpfun.discover()
            GLOBAL["pumpfun_api_ok_cycles" if pf else "pumpfun_api_empty_cycles"] += 1
            for m, (tag, coin) in pf.items():
                COINS[m] = coin
                found[m] = tag
            for src in (sources.ds_discover, sources.jup_discover, sources.rc_discover):
                for m, tag in src().items():
                    if m.endswith("pump"):
                        found.setdefault(m, tag)
            fresh = [m for m in found if time.time() - SEEN.get(m, 0) > G["reevaluate_minutes"] * 60]
            pairs = sources.ds_pairs(fresh) if fresh else {}
            GLOBAL["unique_candidate_mints"] = len(set(SEEN) | set(found))
            for m in fresh:
                if STOP.is_set(): break
                SEEN[m] = time.time()
                c = build_cand(m, pairs.get(m), found[m])
                if c is None:
                    if m not in pairs: GLOBAL["no_dex_pair_yet"] += 1
                    continue
                on_token(c)
            log.info("cycle: %d pump candidates, %d evaluated, regime=%s SOL=$%s (%.0fs)", len(found), len(fresh), REG["why"],
                     REG.get("sol_usd"), time.time() - t0)
        except Exception as e:
            log.exception("discovery error %s", e)
        STOP.wait(max(2, G["discovery_interval_seconds"] - (time.time() - t0)))


LEADERS = collections.defaultdict(dict)   # mint -> {wallet_label: ts}
EVID = {w["label"]: w["evidence"] for w in CFG["wallets"]["follow"]}
SIGQ = queue.Queue()


def signal_worker():
    W = CFG["wallets"]
    while not STOP.is_set():
        try:
            s = SIGQ.get(timeout=1)
        except queue.Empty:
            continue
        try:
            m = s["mint"]
            if s["type"] == "copy_buy":
                GLOBAL["leader_buys"] += 1
                LEADERS[m][s["label"]] = time.time()
                recent = {l: t for l, t in LEADERS[m].items() if time.time() - t < W["multi_whale_window_minutes"] * 60}
                if not m.endswith("pump") and m not in COINS:
                    GLOBAL["leader_buy_not_pump"] += 1
                    continue
                c = build_cand(m, sources.ds_pairs([m]).get(m), "whale:" + s["label"])
                if not c:
                    GLOBAL["leader_buy_no_pair"] += 1
                    log.info("leader %s bought %s: no DEX pair data", s["label"], m)
                    continue
                log.info("leader %s bought %s (%s) [%s] ~%.2f SOL liq=$%.0f; wallets in window=%s", s["label"], c["symbol"], m[:8],
                         c["phase"], s["value_sol"], c["liq"], list(recent))
                SEEN[m] = time.time()
                for acct in ACCTS.values():
                    meta = {"source": "whale_monitor", "wallets": list(recent), "evidence": {l: EVID.get(l) for l in recent},
                            "leader_buy_sol": round(s["value_sol"], 3)}
                    if "B" in acct.a["strategies"] and len(recent) >= CFG["strategy"]["B"]["min_wallets"]:
                        if (c["parts"]["m5"] or 0) > CFG["strategy"]["B"]["max_m5_pump_pct"]:
                            acct.stats[f"B:{c['phase']}"]["signals"] += 1
                            record_decision(acct, "B", c, False, ["entry_chased(m5_pump)"], {})
                        else:
                            decide_and_enter(acct, "B", c, dict(meta, signal="multi_whale"))
                    if "A" in acct.a["strategies"]:
                        if (c["parts"]["m5"] or 0) > CFG["strategy"]["A"]["max_m5_pump_pct"]:
                            acct.stats[f"A:{c['phase']}"]["signals"] += 1
                            record_decision(acct, "A", c, False, ["entry_chased(m5_pump)"], {})
                        else:
                            decide_and_enter(acct, "A", c, dict(meta, wallets=[s["label"]], signal="smart_money"))
                    if "V1" in acct.a["strategies"]:
                        decide_and_enter(acct, "V1", c, dict(meta, wallets=[s["label"]], signal="copy"))
            elif s["type"] == "copy_sell":
                GLOBAL["leader_sells"] += 1
                for acct in ACCTS.values():
                    p = acct.positions.get(m)
                    if p and s["label"] in p["wallets"] and p["strategy"] in ("A", "B", "V1"):
                        close(acct, m, "whale_distribution")
        except Exception as e:
            log.exception("signal error %s", e)


def monitor_loop():
    last = 0
    while not STOP.is_set():
        try:
            held = {m for a in ACCTS.values() for m in a.positions}
            if held:
                pr = sources.jup_prices(held)
                solp = (pr.get(WSOL) or {}).get("usdPrice")
                dsp = sources.ds_pairs(held)
                for acct in list(ACCTS.values()):
                    for m, p in list(acct.positions.items()):
                      try:
                        if not valid_px(p.get("entry_mark")):   # data error: repair from the fill, else close it
                            fix = p["size_sol"] / p["tokens"] if p.get("tokens") else None
                            if valid_px(fix):
                                log.warning("repairing invalid entry_mark=%r for %s %s -> fill price %.3e", p.get("entry_mark"), acct.name, p["symbol"], fix)
                                p["entry_mark"] = fix
                                p["peak_mark"] = max(p["peak_mark"], fix) if valid_px(p.get("peak_mark")) else fix
                                if not valid_px(p.get("last_mark")): p["last_mark"] = fix
                            else:
                                log.warning("closing %s %s: invalid entry price and no fill to repair it", acct.name, p["symbol"])
                                close(acct, m, "data_error_invalid_entry")
                                continue
                        mark = pr[m]["usdPrice"] / solp if (m in pr and solp) else None
                        if mark is None and m in dsp and dsp[m].get("quoteToken", {}).get("address") == WSOL:
                            mark = float(dsp[m].get("priceNative") or 0) or None
                        if not valid_px(mark):
                            mark = None
                        if m in dsp and (dsp[m].get("liquidity") or {}).get("usd"):
                            p["last_liq"] = dsp[m]["liquidity"]["usd"]
                        R = exit_rules(acct, p["strategy"])
                        reason = None
                        if mark:
                            p["last_mark"], p["last_ok"] = mark, time.time()
                            p["peak_mark"] = max(p["peak_mark"], mark)
                            chg, peak = mark / p["entry_mark"] - 1, p["peak_mark"] / p["entry_mark"] - 1
                            be = R.get("breakeven_at_pct")
                            if chg >= R["take_profit_pct"] / 100: reason = "take_profit"
                            elif chg <= -R["stop_loss_pct"] / 100: reason = "stop_loss"
                            elif peak >= R["trailing_activate_pct"] / 100 and mark <= p["peak_mark"] * (1 - R["trailing_stop_pct"] / 100): reason = "trailing_stop"
                            elif be and peak >= be / 100 and chg <= (p["cost_pct_est"] or 0) / 200: reason = "breakeven_stop"
                        elif time.time() - p["last_ok"] > 180:
                            reason = "price_data_lost"
                        if not reason and time.time() - p["opened"] >= R["max_hold_minutes"] * 60: reason = "time_exit"
                        if not reason and R.get("liq_drop_exit_pct") and p["last_liq"] < p["entry_liq"] * (1 - R["liq_drop_exit_pct"] / 100):
                            reason = "liquidity_deterioration"
                        if not reason and p["strategy"] in ("A", "B", "C") and acct.a.get("use_regime_gate") and \
                                ((REG.get("pc") or {}).get("h1") or 0) <= CFG["regime"]["emergency_exit_h1_pct"]:
                            reason = "regime_emergency_exit"
                        if reason:
                            close(acct, m, reason)
                      except Exception as e:   # one bad position must not stop the others from being managed
                        log.exception("position check error %s %s: %s", acct.name, p.get("symbol"), e)
            if time.time() - last > 60:
                last = time.time()
                persist_state()
                log.info("STATUS " + " | ".join(f"{n}: eq={a.equity():.4f} open={len(a.positions)} closed={len(a.closed)}{' HALT:' + a.halted if a.halted else ''}"
                                                for n, a in ACCTS.items()))
        except Exception as e:
            log.exception("monitor error %s", e)
        STOP.wait(G["monitor_interval_seconds"])


# ------------------------------------------------------------ main
update_regime()
if RESUME:
    SOL0, T0 = RESUME["sol0"], RESUME["t0"]
else:
    SOL0 = (sources.jup_prices([]).get(WSOL) or {}).get("usdPrice") or REG.get("sol_usd")
    T0 = time.time()
    with open(RUNF, "w") as f:
        json.dump({"run": RUN, "t0": T0, "sol0": SOL0, "minutes": args.minutes}, f)
log.info("SENTINEL paper run %s; SOL=$%.2f; regime=%s; accounts=%s%s", RUN, SOL0, REG["why"], list(ACCTS),
         f"; resumed with {sum(len(a.positions) for a in ACCTS.values())} open positions" if RESUME else "")
shim = {"rpc": {"url": V1CFG["rpc"]["url"]}, "copytrade": {"wallets": CFG["wallets"]["follow"], "poll_seconds": CFG["wallets"]["poll_seconds"],
        "min_followed_buy_sol": CFG["wallets"]["min_leader_buy_sol"]}}
cw = CopyWatcher(shim, SIGQ.put, GLOBAL)
ths = [threading.Thread(target=f, daemon=True) for f in (discovery_loop, signal_worker, monitor_loop)] + [cw]
for t in ths: t.start()
TERM = threading.Event()
signal.signal(signal.SIGTERM, lambda *a: TERM.set())
try:
    while time.time() - T0 < args.minutes * 60 and not TERM.is_set():
        time.sleep(1)
except KeyboardInterrupt:
    pass
STOP.set(); cw.stop.set()
time.sleep(3)
if TERM.is_set() and args.keep_open_on_exit and time.time() - T0 < args.minutes * 60:
    persist_state()
    log.info("PAUSED run %s for handover: %d open positions saved, nothing sold", RUN, sum(len(a.positions) for a in ACCTS.values()))
    sys.exit(0)
for acct in ACCTS.values():
    for m in list(acct.positions):
        close(acct, m, "end_of_run")
SOL1 = (sources.jup_prices([]).get(WSOL) or {}).get("usdPrice") or REG.get("sol_usd")
from pump_report import write_report
write_report(RUN, ACCTS, GLOBAL, SOL0, SOL1, T0, time.time(), http.STATS, CFG)
for f in (RUNF, ACCTF):   # run finished: the next start begins a fresh run
    if os.path.exists(f): os.remove(f)
