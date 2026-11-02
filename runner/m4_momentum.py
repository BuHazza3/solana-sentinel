#!/usr/bin/env python3
"""M4 MOMENTUM: fourth strategy, PAPER ONLY (no wallet, no keys, no transactions).
Runs as its OWN process next to pump_sentinel.py (which it never touches). Own 1 SOL paper account, own log/CSV/SQLite.

Buys NEW pump.fun tokens (bonding curve or just graduated) that show real volume and trading, after pump.fun-specific
anti-rug checks. Copy-trade boost: a token bought by tracked profitable wallets (pump_sentinel.toml [wallets].follow)
gets a relaxed activity floor; 2+ wallets = strongest signal. Exits:
  stop loss on the full position; at +X% sell just enough to pull the INITIAL SOL (incl. fees) back out, then the stop on
  the rest moves to entry; at 2x sell everything; max hold; dead/illiquid token; tracked leader sells.
Live data: pump.fun API, DexScreener, RugCheck, Jupiter quotes, public RPC. Only the fills are simulated (real Jupiter
quotes + latency slippage + network fees + token-account rent).

  python3 m4_momentum.py            # runs until SIGINT/SIGTERM; restarts resume the same paper account
"""
import argparse, collections, csv, json, logging, os, queue, signal, sqlite3, sys, threading, time, tomllib
from bot import sources, riskscore, http, pumpfun
from bot.copytrade import CopyWatcher

ap = argparse.ArgumentParser()
ap.add_argument("--config", default="m4_momentum.toml")
ap.add_argument("--wallets-config", default="pump_sentinel.toml")
ap.add_argument("--rpc-config", default="config.toml")
ap.add_argument("--new-run", action="store_true", help="start a fresh 1 SOL account instead of resuming")
args = ap.parse_args()
CFG = tomllib.load(open(args.config, "rb"))
WCFG = tomllib.load(open(args.wallets_config, "rb"))
RPCURL = tomllib.load(open(args.rpc_config, "rb"))["rpc"]["url"]
G, RK, EN, CP, AR, EX = CFG["general"], CFG["risk"], CFG["entry"], CFG["copy"], CFG["antirug"], CFG["exit"]
assert G["paper"] is True
os.makedirs("data", exist_ok=True)
ACCT = "M4"
STATE_F, ACCT_F, RUN_F = "data/m4_state.json", "data/m4_account.json", "data/m4_run.txt"
if args.new_run or not os.path.exists(RUN_F):
    open(RUN_F, "w").write(time.strftime("M%Y%m%d-%H%M%S"))
    if os.path.exists(ACCT_F): os.replace(ACCT_F, ACCT_F + ".prev")
RUN = open(RUN_F).read().strip()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-8s %(message)s", datefmt="%H:%M:%S",
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(f"data/m4_momentum_{RUN}.log")])
logging.getLogger("http").setLevel(logging.WARNING)
log = logging.getLogger("m4")
LOCK = threading.RLock()
STOP = threading.Event()
WSOL = sources.WSOL
LAT = G["latency_slippage_bps"] / 1e4
FEE = G["fee_base_sol"] + G["fee_priority_sol"] + G["fee_jito_sol"]
RENT = G["ata_rent_sol"]

db = sqlite3.connect("data/m4_momentum.db", check_same_thread=False)
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
CSV = "data/m4_trades.csv"
CSV_COLS = ["run", "account", "strategy", "phase", "symbol", "mint", "opened", "closed", "hold_s", "size_sol", "sol_out", "fees_sol",
            "pnl_sol", "pnl_pct", "exit_reason", "initials_pulled", "recovered_sol", "tier", "leaders", "rug_score"]
if not os.path.exists(CSV):
    csv.writer(open(CSV, "w", newline="")).writerow(CSV_COLS)


def dbx(sql, params):
    with LOCK:
        db.execute(sql, params)
        db.commit()


GLOBAL = collections.Counter()
MKT = {"sol_usd": None, "ts": 0}

# ------------------------------------------------------------ account (persisted so a restart resumes the same paper run)
A = {"balance": G["start_balance_sol"], "start": G["start_balance_sol"], "positions": {}, "traded": [], "closed_n": 0, "pnl": 0.0}
if os.path.exists(ACCT_F):
    try:
        s = json.load(open(ACCT_F))
        if s.get("run") == RUN:
            A.update({k: s[k] for k in A if k in s})
    except Exception as e:
        print("could not restore account:", e)
TRADED = set(A["traded"])
for _p in A["positions"].values():
    _p["last_ok"] = time.time()   # a restart/handover gap is not "price data lost"
for _p in A["positions"].values():   # cost basis = fill price (older records used the DEX mark)
    if _p.get("fill_mark") and _p["stage"] == "full" and abs(_p["entry_mark"] / _p["fill_mark"] - 1) > 1e-9:
        _p["entry_mark"] = _p["fill_mark"]
        _p["stop_mark"] = _p["fill_mark"] * (1 - CFG["exit"]["stop_loss_pct"] / 100)


def equity():
    return A["balance"] + sum(p["tokens"] * p["last_mark"] + RENT for p in A["positions"].values())


def persist():
    with LOCK:
        A["traded"] = sorted(TRADED)
        tmp = ACCT_F + ".tmp"
        json.dump(dict(A, run=RUN), open(tmp, "w"), default=str)
        os.replace(tmp, ACCT_F)


def record(cand, approved, reasons, detail):
    GLOBAL["decisions"] += 1
    if not approved:
        for r in reasons: GLOBAL["reject:" + r.split("(")[0][:50]] += 1
    dbx("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)", (RUN, time.time(), ACCT, "M4", cand.get("phase"), cand["mint"], cand["symbol"],
                                                             int(approved), ";".join(reasons), json.dumps(detail, default=str)))


# ------------------------------------------------------------ live market helpers
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


def route_check(mint, size_sol):
    qb = sources.jup_quote(WSOL, mint, size_sol * 1e9, G["max_slippage_bps"])
    r = {"size": size_sol, "buy_ok": False, "sell_ok": False}
    if qb and qb.get("outAmount") and int(qb["outAmount"]) > 0:
        r.update(buy_ok=True, tokens_raw=int(qb["outAmount"]), buy_impact_pct=float(qb.get("priceImpactPct") or 0) * 100,
                 route=[x["swapInfo"].get("label") for x in qb.get("routePlan", [])])
        qs = sources.jup_quote(mint, WSOL, r["tokens_raw"], G["max_slippage_bps"])
        if qs and qs.get("outAmount") and int(qs["outAmount"]) > 0:
            r.update(sell_ok=True, round_trip_loss_pct=(1 - int(qs["outAmount"]) / 1e9 / size_sol) * 100)
    return r


def sell_quote(mint, tokens, dec):
    raw = int(tokens * 10 ** dec)
    if raw <= 0:
        return 0.0
    q = sources.jup_quote(mint, WSOL, raw, G["max_slippage_bps"])
    return int(q["outAmount"]) / 1e9 * (1 - LAT) if q and q.get("outAmount") else None


# ------------------------------------------------------------ copy-trade signals (tracked profitable wallets)
LEAD = collections.defaultdict(dict)       # mint -> {label: (ts, sol)}
LEAD_SELL = collections.defaultdict(dict)  # mint -> {label: ts}
PRIO = queue.Queue()


def leaders_of(mint):
    w = CP["window_minutes"] * 60
    return {l: v for l, v in LEAD.get(mint, {}).items() if time.time() - v[0] <= w and v[1] >= CP["min_leader_buy_sol"]}


def on_copy(s):
    m = s["mint"]
    if s["type"] == "copy_buy":
        GLOBAL["leader_buys_seen"] += 1
        LEAD[m][s["label"]] = (time.time(), s["value_sol"])
        if m.endswith("pump") or m in COINS:
            log.info("leader %s bought %s (~%.2f SOL); tracked wallets in %dmin window: %s", s["label"], m, s["value_sol"],
                     CP["window_minutes"], list(leaders_of(m)))
            PRIO.put(m)
    elif s["type"] == "copy_sell":
        GLOBAL["leader_sells_seen"] += 1
        LEAD_SELL[m][s["label"]] = time.time()
        p = A["positions"].get(m)
        if p and CP["exit_when_leader_sells"] and s["label"] in p.get("leaders", []):
            if p["stage"] == "full":
                log.info("leader %s SOLD %s which we hold -> exit", s["label"], p["symbol"])
                EXITQ.put((m, "leader_sold"))
            else:
                p["trail_pct"] = EX["moonbag_trail_after_leader_sell_pct"]
                log.info("leader %s SOLD %s (moonbag) -> trail tightened to %d%%", s["label"], p["symbol"], p["trail_pct"])


# ------------------------------------------------------------ evaluation (deterministic, every rejection logged with reasons)
COINS, LAST_EVAL = {}, {}
_pump_cache = {}
CRIT = {"min_lp_locked_pct": AR["min_lp_locked_pct"], "max_top10_pct": AR["max_top10_pct"], "max_single_pct": AR["max_single_holder_pct"],
        "max_round_trip_loss_pct": EN["max_round_trip_loss_pct"], "veto_clmm_only": True, "max_dev_hold_pct": AR["max_dev_hold_pct"],
        "max_bundle_pct": AR["max_bundle_pct"], "serial_launcher_coins": AR["serial_launcher_coins"]}


def pump_data(mint, creator):
    hit = _pump_cache.get(mint)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    d = {"creator": creator, "dev_history": pumpfun.creator_history(creator) if creator else None,
         "launch": pumpfun.launch_analysis(mint, creator) if creator else {}}
    _pump_cache[mint] = (time.time(), d)
    return d


def evaluate(mint, pair, coin, source):
    LAST_EVAL[mint] = time.time()
    if mint in TRADED or mint in A["positions"]:
        return
    if not pair:
        GLOBAL["no_dex_pair_yet"] += 1
        return
    sym = (pair.get("baseToken") or {}).get("symbol") or (coin or {}).get("symbol") or mint[:6]
    ld = leaders_of(mint)
    tier = "multi_leader" if len(ld) >= 2 else "leader" if ld else "scan"
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
    cand = {"mint": mint, "symbol": sym, "phase": phase}
    max_age = EN["max_age_minutes_multi_leader"] if tier == "multi_leader" else EN["max_age_minutes"]
    # quiet tokens are counted, not logged (keeps the decision log readable)
    if tier == "scan" and (n5 < 20 or (age is not None and age > max_age)):
        GLOBAL["quiet_or_old_skipped"] += 1
        return
    if tier == "scan":
        f_vol, f_n, f_bs, f_m5 = EN["min_vol_5m_usd"], EN["min_trades_5m"], EN["min_buy_sell_ratio_5m"], EN["min_price_change_5m_pct"]
    elif tier == "leader":
        f_vol, f_n, f_bs, f_m5 = CP["leader_min_vol_5m_usd"], CP["leader_min_trades_5m"], CP["leader_min_buy_sell_ratio_5m"], CP["leader_min_price_change_5m_pct"]
    else:
        f_vol, f_n, f_bs, f_m5 = CP["leader_min_vol_5m_usd"], CP["leader_min_trades_5m"], CP["multi_min_buy_sell_ratio_5m"], CP["leader_min_price_change_5m_pct"]
    det = {"tier": tier, "leaders": {l: round(v[1], 2) for l, v in ld.items()}, "age_min": age and round(age, 1), "vol5_usd": vol5,
           "buys5": b5, "sells5": s5, "m5": m5, "h1": h1, "mcap_usd": mc, "ath_usd": ath, "source": source}
    R = []
    # 1. new + real volume/activity + momentum (not vertical, not crashed)
    if age is None: R.append("age_unknown")
    elif age > max_age: R.append(f"too_old({age:.0f}m>{max_age}m)")
    if mc < EN["min_mcap_usd"]: R.append(f"mcap_too_small(${mc:,.0f}<${EN['min_mcap_usd']:,})")
    if mc > EN["max_mcap_usd"]: R.append(f"mcap_too_big(${mc:,.0f})")
    if vol5 < f_vol: R.append(f"low_5m_volume(${vol5:,.0f}<${f_vol:,})")
    if n5 < f_n: R.append(f"few_5m_trades({n5}<{f_n})")
    if b5 < f_bs * max(1, s5): R.append(f"buys_not_beating_sells({b5}/{s5}<{f_bs})")
    if m5 < f_m5: R.append(f"price_not_rising(5m {m5:+.0f}%)")
    if m5 > EN["max_price_change_5m_pct"]: R.append(f"already_vertical(5m +{m5:.0f}%)")
    if h1 > EN["max_price_change_1h_pct"]: R.append(f"already_vertical(1h +{h1:.0f}%)")
    if ath and mc and mc < ath * EN["min_mcap_vs_ath_pct"] / 100: R.append(f"crashed_from_ath({mc / ath * 100:.0f}%_of_ATH)")
    sold = [l for l, t in LEAD_SELL.get(mint, {}).items() if time.time() - t < CP["window_minutes"] * 60]
    if sold: R.append("tracked_wallet_sold(" + ",".join(sold) + ")")
    if (coin or {}).get("is_banned"): R.append("pumpfun_banned")
    if R:
        record(cand, False, R, det)
        return
    # 2. risk limits
    eq = equity()
    if len(A["positions"]) >= RK["max_concurrent"]: R.append("max_concurrent_positions")
    if A["balance"] - RK["position_sol"] - FEE - RENT < RK["min_cash_reserve_sol"]: R.append("insufficient_balance")
    if eq < RK["halt_below_equity_sol"]: R.append(f"risk_breaker:equity_below_{RK['halt_below_equity_sol']}")
    if R:
        record(cand, False, R, det)
        return
    # 3. RugCheck + on-chain authorities + holders + liquidity
    rep = sources.rc_report(mint, ttl=120)
    if not rep:
        record(cand, False, ["no_rugcheck_report"], det)
        return
    if rep.get("mintAuthority") and AR["require_mint_authority_revoked"]: R.append("mint_authority_not_revoked")
    if rep.get("freezeAuthority") and AR["require_freeze_authority_revoked"]: R.append("freeze_authority_not_revoked")
    if rep.get("rugged"): R.append("rugcheck_marked_rugged")
    sn = rep.get("score_normalised")
    det["rc_score"] = sn
    if sn is not None and sn > AR["max_rugcheck_score"]: R.append(f"rugcheck_score({sn}>{AR['max_rugcheck_score']})")
    dangers = [r["name"] for r in rep.get("risks") or [] if r.get("level") == "danger"]
    if dangers and AR["reject_rugcheck_danger"]: R.append("rugcheck_danger:" + "|".join(dangers)[:90])
    t10, t1 = riskscore.holders(rep)
    det["top10_pct"], det["top1_pct"] = t10 and round(t10, 1), t1 and round(t1, 1)
    if t10 is not None and t10 > AR["max_top10_pct"]: R.append(f"top10_holders({t10:.0f}%>{AR['max_top10_pct']}%)")
    if t1 is not None and t1 > AR["max_single_holder_pct"]: R.append(f"single_holder({t1:.0f}%>{AR['max_single_holder_pct']}%)")
    ins = rep.get("graphInsidersDetected") or 0
    if ins > AR["max_insiders"]: R.append(f"insider_network({ins})")
    phase = pumpfun.phase_of(rep, pair, coin)
    cand["phase"] = det["phase"] = phase
    liq = (pair.get("liquidity") or {}).get("usd") or rep.get("totalMarketLiquidity") or 0
    det["liq_usd"] = liq
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
        record(cand, False, R, det)
        return
    # 4. pump.fun dev / launch checks (creator history, dev holding + selling, bundled launch, unique traders)
    creator = (coin or {}).get("creator") or rep.get("creator")
    pd = pump_data(mint, creator)
    dh, la = pd.get("dev_history"), pd.get("launch") or {}
    if dh:
        det["dev_coins"], det["dev_graduated"] = dh["coins"], dh["graduated"]
        if dh["coins"] >= AR["serial_launcher_coins"] and dh["graduated"] == 0:
            R.append(f"serial_launcher({dh['coins']}{'+' if dh.get('capped_at_50') else ''}_coins_0_graduated)")
    if la.get("trades_fetched"):
        dev_net = (la.get("dev_bought_pct_supply") or 0) * (1 - min(100, la.get("dev_sold_pct_of_buys") or 0) / 100)
        det.update(dev_hold_pct=round(dev_net, 2), dev_sold_pct=la.get("dev_sold_pct_of_buys"), unique_traders=la.get("recent_unique_traders"),
                   bundle_pct=la.get("bundle_pct_supply"), launch_block_buyers=la.get("launch_block_buyers"))
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
        record(cand, False, R, det)
        return
    # 5. real Jupiter route (honeypot / exit cost) + pump-adapted rug score
    size = RK["position_sol"]
    route = route_check(mint, size)
    if not route["buy_ok"]: R.append("no_buy_route")
    if not route["sell_ok"]: R.append("no_sell_route(honeypot_risk)")
    elif route["round_trip_loss_pct"] > EN["max_round_trip_loss_pct"]: R.append(f"exit_cost_too_high({route['round_trip_loss_pct']:.1f}%)")
    score, vetoes, comp, unc = riskscore.pump_score(pair, rep, route if route["buy_ok"] else None, CRIT,
                                                    dict(pd, phase=phase, coin=coin, progress=det.get("curve_progress"), age_min=age))
    det.update(rug_score=score, rug_components=comp, route=route)
    R += ["veto:" + v for v in vetoes if not any(v.split("(")[0] in r for r in R)]
    if score > EN["max_pump_rug_score"]: R.append(f"rug_score({score}>{EN['max_pump_rug_score']})")
    if R:
        record(cand, False, R, det)
        return
    enter(cand, pair, rep, route, det, liq, score, tier, ld)


def enter(cand, pair, rep, route, det, liq, score, tier, ld):
    with LOCK:
        if len(A["positions"]) >= RK["max_concurrent"]:
            record(cand, False, ["max_concurrent_positions"], det); return
        dec = ((rep or {}).get("token") or {}).get("decimals") or 6
        size = RK["position_sol"]
        tokens = route["tokens_raw"] / 10 ** dec * (1 - LAT)
        ds_mark = mark_of(pair)
        mark = size / tokens            # cost basis = the real Jupiter fill price (incl. latency slippage), not the lagging DEX mark
        if ds_mark and mark > ds_mark * (1 + EN["max_fill_vs_mark_pct"] / 100):
            record(cand, False, [f"price_ran_since_data(fill_{(mark / ds_mark - 1) * 100:.0f}%_above_mark)"], det); return
        A["balance"] -= size + FEE + RENT
        pos = dict(mint=cand["mint"], symbol=cand["symbol"], phase=cand["phase"], decimals=dec, opened=time.time(), size_sol=size,
                   tokens=tokens, tokens0=tokens, entry_mark=mark, fill_mark=size / tokens, last_mark=mark, peak_mark=mark, last_ok=time.time(),
                   entry_liq=liq, last_liq=liq, fees=FEE, stage="full", stop_mark=mark * (1 - EX["stop_loss_pct"] / 100),
                   recovered_sol=0.0, sells=[], leaders=list(ld), tier=tier, rug_score=score, n5_last=None,
                   value_sol=None, value_ts=0)
        A["positions"][cand["mint"]] = pos
        TRADED.add(cand["mint"])
        det["size"] = size
        det["signal"] = tier
        record(cand, True, [], det)
        persist()
        log.info("ENTER [%s] M4 %-10s size=%.4f SOL tier=%s leaders=%s age=%sm vol5=$%.0f b/s=%s/%s m5=%+.0f%% rug=%s rt=%.1f%% mark=%.3e",
                 cand["phase"], cand["symbol"], size, tier, list(ld), det.get("age_min"), det["vol5_usd"], det["buys5"], det["sells5"],
                 det["m5"], score, route.get("round_trip_loss_pct", -1), mark)


# ------------------------------------------------------------ selling (partial = initials pulled, final = everything left)
EXITQ = queue.Queue()


def sell(mint, frac, reason, final):
    with LOCK:
        p = A["positions"].get(mint)
        if not p:
            return False
        tok = p["tokens"] if final else p["tokens"] * frac
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
        kind = "final" if final else "initials"
        p["sells"].append({"ts": time.time(), "kind": kind, "frac": tok / p["tokens0"], "sol_out": out, "mark": p["last_mark"], "reason": reason})
        dbx("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (RUN, ACCT, mint, p["symbol"], p["opened"], time.time(), kind, tok / p["tokens0"],
                                                                         out, FEE, p["last_mark"], p["recovered_sol"], p["size_sol"], int(final), reason))
        if not final:
            p["stage"] = "initials_pulled"
            p["initials_ts"] = time.time()
            p["moon_peak"] = p["last_mark"]
            p["trail_pct"] = EX["moonbag_trail_pct"]
            p["stop_mark"] = max(p["entry_mark"] if EX["breakeven_after_initials"] else 0, p["moon_peak"] * (1 - p["trail_pct"] / 100))
            log.info("PARTIAL [%s] M4 %-10s initials pulled: sold %.0f%% for %.5f SOL (in %.4f + fees %.5f) at %.2fx; moonbag %.0f%% left, trail %d%% (stop >= entry), cap %.1fx",
                     p["phase"], p["symbol"], tok / p["tokens0"] * 100, out, p["size_sol"], p["fees"], p["last_mark"] / p["entry_mark"],
                     p["tokens"] / p["tokens0"] * 100, p["trail_pct"], EX["moonbag_cap_x"])
            persist()
            return True
        A["balance"] += RENT
        del A["positions"][mint]
        closed = time.time()
        sol_out = p["recovered_sol"]
        pnl = sol_out - p["size_sol"] - p["fees"]
        exit_slip = 0.0
        rec = dict(p, closed=closed, sol_out=sol_out, exit_reason=reason, pnl_sol=pnl, pnl_pct=pnl / p["size_sol"] * 100, exit_mark=p["last_mark"],
                   exit_plan=plan(), initials_pulled=p["stage"] == "initials_pulled", strategy="M4", account=ACCT)
        A["closed_n"] += 1
        A["pnl"] += pnl
        db_cost = p["fees"] / p["size_sol"] * 100
        dbx("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (RUN, ACCT, "M4", p["phase"], mint, p["symbol"], p["opened"], closed, p["size_sol"], sol_out, p["fees"], exit_slip, pnl, rec["pnl_pct"],
             db_cost, reason, json.dumps(rec, default=str)))
        csv.writer(open(CSV, "a", newline="")).writerow(
            [RUN, ACCT, "M4", p["phase"], p["symbol"], mint, round(p["opened"]), round(closed), round(closed - p["opened"]), p["size_sol"],
             round(sol_out, 6), round(p["fees"], 6), round(pnl, 6), round(rec["pnl_pct"], 2), reason, int(rec["initials_pulled"]),
             round(p["recovered_sol"], 6), p["tier"], "|".join(p["leaders"]), p["rug_score"]])
        log.info("EXIT  [%s] M4 %-10s %s pnl=%+.5f SOL (%+.1f%%) hold=%ds initials_pulled=%s bal=%.4f", p["phase"], p["symbol"], reason, pnl,
                 rec["pnl_pct"], closed - p["opened"], rec["initials_pulled"], A["balance"])
        persist()
        return True


def pull_initials(mint):
    """Sell just enough to get back the SOL put in + all fees (buy, this sell, and the final sell)."""
    p = A["positions"][mint]
    target = p["size_sol"] + p["fees"] + 2 * FEE
    est = p["tokens"] * p["last_mark"] * (1 - LAT)
    if est <= 0:
        return False
    frac = min(0.9, target / est * 1.03)
    out = sell_quote(mint, p["tokens"] * frac, p["decimals"])
    if out is None:
        return False
    if out < target and frac < 0.9:
        frac = min(0.9, frac * target / max(out, 1e-9) * 1.02)
    return sell(mint, frac, "initials_pulled", False)


def plan():
    return {"stop_loss_pct": EX["stop_loss_pct"], "take_profit_pct": round((EX["initials_at_x"] - 1) * 100), "initials_at_x": EX["initials_at_x"],
            "moonbag_trail_pct": EX["moonbag_trail_pct"], "moonbag_cap_x": EX["moonbag_cap_x"], "breakeven_after_initials": EX["breakeven_after_initials"],
            "max_hold_minutes": EX["max_hold_minutes"], "moonbag_max_hold_minutes": EX["moonbag_max_hold_minutes"], "liq_drop_exit_pct": EX["liq_drop_exit_pct"]}


def monitor_loop():
    last_status, last_eq = 0, 0
    while not STOP.is_set():
        t0 = time.time()
        try:
            update_sol()
            while not EXITQ.empty():
                m, why = EXITQ.get()
                if not sell(m, 1, why, True) and m in A["positions"]:
                    A["positions"][m]["pending_exit"] = why
            held = list(A["positions"])
            dsp = sources.ds_pairs(held) if held else {}
            for m in held:
                p = A["positions"].get(m)
                if not p:
                    continue
                pr = dsp.get(m)
                mark = mark_of(pr)
                reason = None
                if pr:
                    if (pr.get("liquidity") or {}).get("usd"): p["last_liq"] = pr["liquidity"]["usd"]
                    tx = (pr.get("txns") or {}).get("m5") or {}
                    p["n5_last"] = tx.get("buys", 0) + tx.get("sells", 0)
                if mark:
                    p["last_mark"], p["last_ok"] = mark, time.time()
                    p["peak_mark"] = max(p["peak_mark"], mark)
                    x = mark / p["entry_mark"]
                    if p["stage"] == "initials_pulled":
                        p["moon_peak"] = max(p.get("moon_peak") or mark, mark)
                        p["stop_mark"] = max(p["entry_mark"] if EX["breakeven_after_initials"] else 0, p["moon_peak"] * (1 - p["trail_pct"] / 100))
                        if x >= EX["moonbag_cap_x"]: reason = f"moonbag_cap_{EX['moonbag_cap_x']:g}x"
                        elif mark <= p["stop_mark"]:
                            reason = "moonbag_breakeven_stop" if p["stop_mark"] <= p["entry_mark"] * 1.0001 else "moonbag_trailing_stop"
                    elif mark <= p["stop_mark"]: reason = "stop_loss"
                    elif x >= EX["initials_at_x"]:
                        pull_initials(m)
                        continue
                elif time.time() - p["last_ok"] > EX["price_lost_seconds"]:
                    reason = "price_data_lost"
                held_s = time.time() - p["opened"]
                if not reason and p.get("pending_exit"): reason = p["pending_exit"]
                mh = EX["moonbag_max_hold_minutes"] if p["stage"] == "initials_pulled" else EX["max_hold_minutes"]
                if not reason and held_s >= mh * 60: reason = "moonbag_time_exit" if p["stage"] == "initials_pulled" else "time_exit"
                if not reason and p["entry_liq"] and p["phase"] == "graduated" and p["last_liq"] < p["entry_liq"] * (1 - EX["liq_drop_exit_pct"] / 100):
                    reason = "liquidity_drop"
                if not reason and held_s >= EX["dead_after_minutes"] * 60 and p["n5_last"] is not None and p["n5_last"] < EX["dead_max_trades_5m"]:
                    reason = "dead_token" if p["stage"] == "full" else "moonbag_volume_died"
                if reason:
                    sell(m, 1, reason, True)
                elif time.time() - p["value_ts"] > 15:   # Jupiter sell quote for the app (what the rest would fetch now)
                    v = sell_quote(m, p["tokens"], p["decimals"])
                    p["value_ts"] = time.time()
                    if v is not None: p["value_sol"] = v
            eq = equity()
            if time.time() - last_eq > 30:
                last_eq = time.time()
                dbx("INSERT INTO equity VALUES (?,?,?,?,?)", (RUN, time.time(), eq, A["balance"], len(A["positions"])))
            if time.time() - last_status > 60:
                last_status = time.time()
                log.info("STATUS M4: eq=%.4f open=%d closed=%d | bal=%.4f decisions=%d quiet_skipped=%d leader_buys=%d", eq, len(A["positions"]),
                         A["closed_n"], A["balance"], GLOBAL["decisions"], GLOBAL["quiet_or_old_skipped"], GLOBAL["leader_buys_seen"])
                persist()
            write_state(eq)
        except Exception as e:
            log.exception("monitor error %s", e)
        STOP.wait(max(0.5, G["monitor_interval_seconds"] - (time.time() - t0)))


def write_state(eq):
    with LOCK:
        hs = []
        for p in A["positions"].values():
            hs.append({k: p.get(k) for k in ("mint", "symbol", "phase", "opened", "size_sol", "tokens", "tokens0", "decimals", "entry_mark", "last_mark",
                                              "peak_mark", "stop_mark", "stage", "moon_peak", "trail_pct", "recovered_sol", "fees", "leaders", "tier", "rug_score", "value_sol",
                                              "value_ts", "initials_ts")})
        st = {"run": RUN, "ts": time.time(), "pid": os.getpid(), "account": ACCT, "equity": eq, "balance": A["balance"], "start": A["start"],
              "open": len(hs), "closed_n": A["closed_n"], "pnl": A["pnl"], "holdings": hs, "sol_usd": MKT["sol_usd"], "plan": plan(),
              "halted": f"equity below {RK['halt_below_equity_sol']} SOL" if eq < RK["halt_below_equity_sol"] else None,
              "counters": dict(GLOBAL.most_common(40)), "last_cycle": CYCLE, "config": {"entry": EN, "copy": CP, "antirug": AR, "exit": EX, "risk": RK}}
    tmp = STATE_F + ".tmp"
    json.dump(st, open(tmp, "w"), default=str)
    os.replace(tmp, STATE_F)


CYCLE = {}


def discovery_loop():
    while not STOP.is_set():
        t0 = time.time()
        try:
            update_sol()
            found = {}
            for tag, q in (("pf_new", "sort=created_timestamp&order=DESC"), ("pf_active", "sort=last_trade_timestamp&order=DESC")):
                d = http.get(f"{pumpfun.PF}/coins?offset=0&limit=50&{q}&includeNsfw=false") or []
                for c in d if isinstance(d, list) else []:
                    if not c.get("mint"): continue
                    COINS[c["mint"]] = c
                    age = (time.time() * 1000 - (c.get("created_timestamp") or 0)) / 60000
                    if age <= EN["max_age_minutes_multi_leader"]:
                        found.setdefault(c["mint"], tag)
            if not found:   # pump.fun API unreachable (e.g. blocked from this network): fall back to Jupiter/DexScreener new-token lists
                GLOBAL["pumpfun_api_empty_cycles"] += 1
                for src in (sources.jup_discover, sources.ds_discover):
                    for mm, tag in (src() or {}).items():
                        if mm.endswith("pump"):
                            found.setdefault(mm, tag + "_fallback")
            for m in list(LEAD):
                if leaders_of(m): found.setdefault(m, "copy_wallet")
            due = [m for m in found if m not in TRADED and time.time() - LAST_EVAL.get(m, 0) >= G["reevaluate_seconds"]]
            pairs = sources.ds_pairs(due) if due else {}
            for m in due:
                if STOP.is_set(): break
                drain_prio()
                evaluate(m, pairs.get(m), COINS.get(m), found[m])
            CYCLE.update(ts=time.time(), found=len(found), evaluated=len(due), secs=round(time.time() - t0, 1))
            log.info("cycle: %d new pump tokens, %d evaluated, open=%d (%.0fs)", len(found), len(due), len(A["positions"]), time.time() - t0)
        except Exception as e:
            log.exception("discovery error %s", e)
        end = time.time() + max(2, G["discovery_interval_seconds"] - (time.time() - t0))
        while time.time() < end and not STOP.is_set():
            drain_prio()
            STOP.wait(1)


def drain_prio():
    while not PRIO.empty():
        m = PRIO.get()
        if m in TRADED or time.time() - LAST_EVAL.get(m, 0) < 20:
            continue
        try:
            evaluate(m, sources.ds_pairs([m]).get(m), COINS.get(m), "copy_wallet")
        except Exception as e:
            log.exception("priority eval error %s", e)


# ------------------------------------------------------------ main
log.info("M4 MOMENTUM paper run %s (pid %d); resumed=%s balance=%.4f open=%d; size=%.2f SOL max %d open; SL -%d%%, initials at %.1fx, "
         "moonbag trail %d%% (stop>=entry), cap %.1fx, max hold %d/%d min", RUN, os.getpid(), bool(A["positions"] or A["closed_n"]), A["balance"],
         len(A["positions"]), RK["position_sol"], RK["max_concurrent"], EX["stop_loss_pct"], EX["initials_at_x"], EX["moonbag_trail_pct"],
         EX["moonbag_cap_x"], EX["max_hold_minutes"], EX["moonbag_max_hold_minutes"])
cw = None
if CP["enabled"]:
    shim = {"rpc": {"url": RPCURL}, "copytrade": {"wallets": WCFG["wallets"]["follow"], "poll_seconds": CP["poll_seconds"],
            "min_followed_buy_sol": CP["min_leader_buy_sol"]}}
    cw = CopyWatcher(shim, on_copy, GLOBAL)
    cw.start()
ths = [threading.Thread(target=f, daemon=True) for f in (discovery_loop, monitor_loop)]
for t in ths: t.start()
TERM = threading.Event()
signal.signal(signal.SIGTERM, lambda *a: TERM.set())
try:
    while not TERM.is_set():
        time.sleep(1)
except KeyboardInterrupt:
    pass
# open positions are kept (persisted) so a restart resumes them; nothing is force-sold on a restart
STOP.set()
if cw: cw.stop.set()
time.sleep(2)
persist()
log.info("M4 stopped; %d open positions kept for the next start", len(A["positions"]))
