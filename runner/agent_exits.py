"""Exit engines of the sub-agents, as pure functions (no network), so each sub-agent keeps the exits it had in its
original bot. PAPER ONLY. Prices are SOL per token. A position is a dict; the engine updates it and returns actions:
  ("final", reason)            sell everything left
  ("partial", frac, reason)    sell frac of the ORIGINAL token amount (S5 card take-profit ladder)
  ("initials", reason)         M4: sell just enough to get the SOL put in (incl. fees) back
classic = pump_sentinel.py monitor_loop (V1 / C / LS / copy); m4 = m4_momentum.py monitor_loop; card = s5_core.Position."""
import s5_core as C


def classic(p, X, now, mark, regime_h1=None, regime=None):
    """TP / SL / trailing / breakeven / time / liquidity / regime emergency, exactly as in pump_sentinel.py."""
    reason = None
    if mark:
        p["last_mark"], p["last_ok"] = mark, now
        p["peak_mark"] = max(p["peak_mark"], mark)
        chg, peak = mark / p["entry_mark"] - 1, p["peak_mark"] / p["entry_mark"] - 1
        be = X.get("breakeven_at_pct")
        if chg >= X["take_profit_pct"] / 100: reason = "take_profit"
        elif chg <= -X["stop_loss_pct"] / 100: reason = "stop_loss"
        elif X.get("trailing_activate_pct") is not None and peak >= X["trailing_activate_pct"] / 100 and mark <= p["peak_mark"] * (1 - X["trailing_stop_pct"] / 100):
            reason = "trailing_stop"
        elif be and peak >= be / 100 and chg <= (p.get("cost_pct_est") or 0) / 200: reason = "breakeven_stop"
    elif now - p["last_ok"] > 180:
        reason = "price_data_lost"
    if not reason and now - p["opened"] >= X["max_hold_minutes"] * 60: reason = "time_exit"
    if not reason and X.get("liq_drop_exit_pct") and p.get("entry_liq") and (p.get("last_liq") or 0) < p["entry_liq"] * (1 - X["liq_drop_exit_pct"] / 100):
        reason = "liquidity_deterioration"
    if not reason and regime and regime_h1 is not None and regime_h1 <= regime["emergency_exit_h1_pct"]:
        reason = "regime_emergency_exit"
    return [("final", reason)] if reason else []


def m4_stop(p, X):
    if p["stage"] == "initials_pulled":
        return max(p["entry_mark"] if X["breakeven_after_initials"] else 0, p["moon_peak"] * (1 - p["trail_pct"] / 100))
    return p["entry_mark"] * (1 - X["stop_loss_pct"] / 100)


def m4(p, X, now, mark, n5=None):
    """Stop -20%; at 2x pull initials; moonbag trail 30% (never below entry), cap 5x; time / dead / liquidity, as in m4_momentum.py."""
    reason = None
    if n5 is not None:
        p["n5_last"] = n5
    if mark:
        p["last_mark"], p["last_ok"] = mark, now
        p["peak_mark"] = max(p["peak_mark"], mark)
        x = mark / p["entry_mark"]
        if p["stage"] == "initials_pulled":
            p["moon_peak"] = max(p.get("moon_peak") or mark, mark)
            p["stop_mark"] = m4_stop(p, X)
            if x >= X["moonbag_cap_x"]: reason = f"moonbag_cap_{X['moonbag_cap_x']:g}x"
            elif mark <= p["stop_mark"]:
                reason = "moonbag_breakeven_stop" if p["stop_mark"] <= p["entry_mark"] * 1.0001 else "moonbag_trailing_stop"
        else:
            p["stop_mark"] = m4_stop(p, X)
            if mark <= p["stop_mark"]: reason = "stop_loss"
            elif x >= X["initials_at_x"]: return [("initials", "initials_pulled")]
    elif now - p["last_ok"] > X["price_lost_seconds"]:
        reason = "price_data_lost"
    held = now - p["opened"]
    mh = X["moonbag_max_hold_minutes"] if p["stage"] == "initials_pulled" else X["max_hold_minutes"]
    if not reason and held >= mh * 60: reason = "moonbag_time_exit" if p["stage"] == "initials_pulled" else "time_exit"
    if not reason and p.get("entry_liq") and p["phase"] == "graduated" and (p.get("last_liq") or 0) < p["entry_liq"] * (1 - X["liq_drop_exit_pct"] / 100):
        reason = "liquidity_drop"
    if not reason and held >= X["dead_after_minutes"] * 60 and p.get("n5_last") is not None and p["n5_last"] < X["dead_max_trades_5m"]:
        reason = "dead_token" if p["stage"] == "full" else "moonbag_volume_died"
    return [("final", reason)] if reason else []


def m4_after_initials(p, X):
    p["stage"] = "initials_pulled"
    p["moon_peak"] = p["last_mark"]
    p["trail_pct"] = X["moonbag_trail_pct"]
    p["stop_mark"] = m4_stop(p, X)


def card_of(X):
    return dict(stop_pct=X["stop_loss_pct"], tp=[tuple(x) for x in X["take_profits"]], trail_pct=X["trail_pct"], trail_arm_x=X.get("trail_arm_x"), trail_after_tp_pct=X.get("trail_after_tp_pct"), timeout_after_tp_min=X.get("max_hold_after_tp_minutes"),
                timeout_min=X["max_hold_minutes"], close_below_entry=False, exit_on_graduation=False, exit_after_grad_min=None)


def card(p, X, now, mark, phase):
    """S5 strategy card (s5_core.Position): stop, take-profit ladder, trail after TP1, timeout."""
    e = C.Position.from_state(p["engine"], card_of(X))
    out = []
    if mark:
        p["last_mark"], p["last_ok"] = mark, now
        p["peak_mark"] = max(p["peak_mark"], mark)
        for frac, px, why in e.tick(now, mark, phase):
            out.append(("final", why) if e.closed else ("partial", frac, why))
    elif now - p["last_ok"] > X["price_lost_seconds"]:
        e.closed = True; out.append(("final", "price_data_lost"))
    elif now - p["opened"] >= ((X.get("max_hold_after_tp_minutes") if e.tp_done else None) or X["max_hold_minutes"]) * 60:
        e.closed = True; out.append(("final", "time_exit"))
    p["engine_next"] = e.state()
    p["stop_mark"] = e._stop_px()
    p["tp_done"] = e.tp_done
    return out


def plan(X):
    """Exit plan for the app (same keys the app already knows)."""
    if X["engine"] == "m4":
        return {k: X[k] for k in ("stop_loss_pct", "initials_at_x", "moonbag_trail_pct", "moonbag_cap_x", "breakeven_after_initials", "max_hold_minutes",
                                  "moonbag_max_hold_minutes", "liq_drop_exit_pct")} | {"take_profit_pct": round((X["initials_at_x"] - 1) * 100)}
    if X["engine"] == "card":
        return {"stop_loss_pct": X["stop_loss_pct"], "take_profit_pct": round((X["take_profits"][0][0] - 1) * 100), "take_profits": X["take_profits"],
                "trailing_stop_pct": X["trail_pct"], "trailing_activate_pct": round(((X.get("trail_arm_x") or X["take_profits"][0][0]) - 1) * 100), "max_hold_minutes": X["max_hold_minutes"]}
    return {k: X[k] for k in ("take_profit_pct", "stop_loss_pct", "breakeven_at_pct", "trailing_activate_pct", "trailing_stop_pct", "max_hold_minutes", "liq_drop_exit_pct") if k in X}
