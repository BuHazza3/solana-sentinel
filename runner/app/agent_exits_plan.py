"""Exit plan summary for the app (same as agent_exits.plan in the bot; kept here so the app imports no bot code)."""


def plan(X):
    if X["engine"] == "m4":
        return {k: X[k] for k in ("stop_loss_pct", "initials_at_x", "moonbag_trail_pct", "moonbag_cap_x", "breakeven_after_initials", "max_hold_minutes",
                                  "moonbag_max_hold_minutes", "liq_drop_exit_pct")} | {"take_profit_pct": round((X["initials_at_x"] - 1) * 100)}
    if X["engine"] == "card":
        return {"stop_loss_pct": X["stop_loss_pct"], "take_profit_pct": round((X["take_profits"][0][0] - 1) * 100), "take_profits": X["take_profits"],
                "trailing_stop_pct": X["trail_pct"], "trailing_activate_pct": round(((X.get("trail_arm_x") or X["take_profits"][0][0]) - 1) * 100), "max_hold_minutes": X["max_hold_minutes"]}
    return {k: X[k] for k in ("take_profit_pct", "stop_loss_pct", "breakeven_at_pct", "trailing_activate_pct", "trailing_stop_pct", "max_hold_minutes", "liq_drop_exit_pct") if k in X}
