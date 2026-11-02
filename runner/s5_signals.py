"""S5 signals: the scouted strategies' exact entry rules over a token's 1-minute candles, plus each strategy card's exits.
Shared by the backtest (/workspace/s5_research) and the live paper bot (s5_quant.py) so both run the SAME rules.
A candle is (ts_minute_start, open, high, low, close, volume_sol, phase) with prices in SOL; tok = {"c": [...], "created", "seen", "grad"}.
Signals only read candles up to and including index i (closed candles): no lookahead."""
import s5_core as C


def sol_at(ts):                     # set by the caller (backtest: historical SOL/USD; live: current SOL/USD)
    return SOL_USD_FN(ts)


SOL_USD_FN = lambda ts: 90.0

# ------------------------------------------------------------------ helpers on a token's candle list
def last_close_before(cs, i, t):
    """Close of the last candle that ENDED at or before time t (candles are [ts, ts+60)). None if none."""
    j = i
    while j >= 0 and cs[j][0] + 60 > t:
        j -= 1
    return cs[j][4] if j >= 0 else None


def net_inflow_sol(cs, i, window_s):
    """Bonding curve: net SOL that entered the curve in the window = change of virtual SOL (exact from price)."""
    if cs[i][6] != "bonding":
        return None
    t_end = cs[i][0] + 60
    p0 = last_close_before(cs, i, t_end - window_s)
    if p0 is None:
        p0 = C.CURVE_V_SOL0 / C.CURVE_V_TOK0
    return C.curve_virtual_sol(cs[i][4]) - C.curve_virtual_sol(p0)


def window(cs, i, secs):
    t = cs[i][0] + 60 - secs
    j = i
    while j - 1 >= 0 and cs[j - 1][0] >= t:
        j -= 1
    return cs[j:i + 1]


# ------------------------------------------------------------------ strategies (exact rules, sources in CARDS)
def sig_graduation_lane(tok):          # Outrider "Graduation" lane
    cs = tok["c"]
    for i, x in enumerate(cs):
        if x[6] != "bonding": break
        pr = C.curve_progress(x[4])
        if 80 <= pr <= 95:
            p2 = last_close_before(cs, i, x[0] + 60 - 120) or C.CURVE_V_SOL0 / C.CURVE_V_TOK0
            if pr - C.curve_progress(p2) >= 5 and (net_inflow_sol(cs, i, 120) or 0) >= 8:
                yield i


def sig_migration_snipe(tok):          # Subglow migration sniper template (wallet gate not reproducible)
    for i, x in enumerate(tok["c"]):
        if x[6] != "bonding": break
        if 85 <= C.curve_progress(x[4]) < 100:
            yield i; return


def sig_dip_migrated(tok):             # Mew-X "Dip" on migrated tokens: -20% then stable
    cs = tok["c"]
    for i in range(1, len(cs)):
        x, prev = cs[i], cs[i - 1]
        if x[6] != "graduated" or prev[6] != "graduated": continue
        if x[0] - prev[0] > 60: continue
        hi = max(y[2] for y in window(cs, i - 1, 300))
        if prev[4] <= hi * 0.80 and x[3] >= prev[4] * 0.98 and x[4] * C.SUPPLY >= 20:   # dip candle, then a minute holding the low
            yield i


def make_revival(min_age_min=30, quiet_window_min=15, quiet_max_sol=1.0, wake_min_sol=4.0, max_vs_peak=0.5):
    """Outrider "Revival" lane: a coin >= 30 min old, quiet for 15 min, wakes up with >= 4 SOL bought (volume and net
    inflow in the last closed minute) while >= 50% below its peak. Net inflow: exact on the bonding curve (change of the
    curve's virtual SOL), estimated for PumpSwap pools (constant product from graduation)."""
    def sig(tok):
        cs = tok["c"]
        peak = 0
        for i, x in enumerate(cs):
            if i > 0: peak = max(peak, cs[i - 1][2])
            if x[0] - tok["created"] < min_age_min * 60 or i == 0: continue
            quiet = sum(y[5] for y in cs[:i] if x[0] - quiet_window_min * 60 <= y[0] < x[0])
            if x[6] == "bonding":
                inflow = net_inflow_sol(cs, i, 60)
            else:
                inflow = C.quote_reserve_sol(x[4], "graduated") - C.quote_reserve_sol(cs[i - 1][4], "graduated")
            if quiet <= quiet_max_sol and x[5] >= wake_min_sol and (inflow or 0) >= wake_min_sol and x[4] <= max_vs_peak * peak:
                yield i
    return sig


sig_revival = make_revival()


def sig_core_lane(tok):                # Outrider pump.fun core gate (curve 5-60%, >=20 SOL net inflow)
    cs = tok["c"]
    for i, x in enumerate(cs):
        if x[6] != "bonding": break
        if 5 <= C.curve_progress(x[4]) <= 60 and (net_inflow_sol(cs, i, 300) or 0) >= 20:
            yield i


def sig_volume_high_breakout(tok):     # Fullport: first higher high after a new high in volume
    cs = tok["c"]
    vmax = 0
    for i, x in enumerate(cs):
        if i >= 3 and x[5] > vmax:
            for j in range(i + 1, len(cs)):
                if cs[j][0] - x[0] > 300: break
                if cs[j][2] > x[2]:
                    yield j; break
        vmax = max(vmax, x[5])


def sig_post_graduation(tok):          # Launch Tape example: buy right after graduation, out 3 min after graduation or -20%
    for i, x in enumerate(tok["c"]):
        if x[6] == "graduated":
            yield i; return


def sig_mean_reversion(tok):           # MemeAI "Mean Reversion Trader": bought well below recent high
    cs = tok["c"]
    for i, x in enumerate(cs):
        if x[0] - tok["created"] < 600: continue
        hi = max(y[2] for y in window(cs, i, 3600))
        if x[4] <= 0.60 * hi:
            yield i


def sig_survivor_30m(tok):             # arrowalgo lab gate: 30 min old, >= $5k liquidity, >= 20 trades in the past hour
    cs = tok["c"]
    for i, x in enumerate(cs):
        if x[0] - tok["created"] >= 1800:
            liq_usd = (C.curve_virtual_sol(x[4]) - C.CURVE_V_SOL0) * 2 * sol_at(x[0]) if x[6] == "bonding" else C.quote_reserve_sol(x[4], "graduated") * 2 * sol_at(x[0])
            active = len(window(cs, i, 3600))
            if liq_usd >= 5000 and active >= 10:
                yield i
            return


def sig_late_buyer(tok):               # accompli study: first trade >= 60 s after creation, take 2x else hold
    for i, x in enumerate(tok["c"]):
        if x[0] >= tok["created"] + 60:
            yield i; return


STRATS = {
    "GRAD_LANE": (sig_graduation_lane, dict(stop_pct=12, tp=[], trail_pct=None, trail_arm_x=None, timeout_min=20, close_below_entry=False, exit_on_graduation=True, exit_after_grad_min=None)),
    "MIGRATION_SNIPE": (sig_migration_snipe, dict(stop_pct=25, tp=[(1.6, 1.0)], trail_pct=15, trail_arm_x=1.0, timeout_min=None, close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=None)),
    "DIP_MIGRATED": (sig_dip_migrated, dict(stop_pct=15, tp=[(1.4, 1.0)], trail_pct=None, trail_arm_x=None, timeout_min=None, close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=None)),
    "REVIVAL": (sig_revival, dict(stop_pct=15, tp=[(1.5, 0.5), (2.5, 1.0)], trail_pct=20, trail_arm_x=None, timeout_min=90, close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=None)),
    "CORE_LANE": (sig_core_lane, dict(stop_pct=12, tp=[(1.3, 0.6), (2.0, 1.0)], trail_pct=15, trail_arm_x=1.3, timeout_min=10, close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=None)),
    "VOL_HIGH_BREAKOUT": (sig_volume_high_breakout, dict(stop_pct=None, tp=[(1.4, 0.5)], trail_pct=None, trail_arm_x=None, timeout_min=30, close_below_entry=True, exit_on_graduation=False, exit_after_grad_min=None)),
    "POST_GRAD_3MIN": (sig_post_graduation, dict(stop_pct=20, tp=[], trail_pct=None, trail_arm_x=None, timeout_min=None, close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=3)),
    "MEAN_REVERSION": (sig_mean_reversion, dict(stop_pct=10, tp=[(1.4, 1.0)], trail_pct=None, trail_arm_x=None, timeout_min=None, close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=None)),
    "SURVIVOR_30M": (sig_survivor_30m, dict(stop_pct=None, tp=[], trail_pct=None, trail_arm_x=None, timeout_min=60, close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=None)),
    "LATE_BUYER_2X": (sig_late_buyer, dict(stop_pct=None, tp=[(2.0, 1.0)], trail_pct=None, trail_arm_x=None, timeout_min=360, close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=None)),
}




# ------------------------------------------------------------------ live helpers (used by s5_quant.py)
def jup_to_tok(raw_candles, sol_usd, created_ts, graduated, grad_ts=None):
    """Jupiter 1-minute USD candles -> token dict in SOL. Phase: graduated candles from grad_ts (or, if unknown and the
    coin is complete, only the latest candle is treated as graduated for the inflow estimate)."""
    cs = []
    for x in sorted(raw_candles, key=lambda x: x["time"]):
        ts = x["time"]
        ph = "graduated" if graduated and (grad_ts is None or ts >= grad_ts - 30) else "bonding"
        cs.append((ts, x["open"] / sol_usd, x["high"] / sol_usd, x["low"] / sol_usd, x["close"] / sol_usd, (x.get("volume") or 0) / sol_usd, ph))
    return {"c": cs, "created": created_ts, "seen": 0, "grad": grad_ts}


def fires_now(sig_fn, tok, now, max_age_s=90):
    """True if the signal fires on the LAST CLOSED candle (closed within max_age_s). The still-open minute is dropped."""
    cs = [x for x in tok["c"] if x[0] + 60 <= now]
    if not cs or now - (cs[-1][0] + 60) > max_age_s:
        return False
    t = dict(tok, c=cs)
    last = len(cs) - 1
    return any(i == last for i in sig_fn(t))
