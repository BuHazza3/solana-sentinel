"""Exit engines kept from the original bots. Run: python3 -m pytest -q tests/"""
import os, sys, tomllib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent_exits as E
import s5_core as C

A = tomllib.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents.toml"), "rb"))["agent"]


def pos(now=0, entry=1.0, **kw):
    return dict(entry_mark=entry, peak_mark=entry, last_mark=entry, last_ok=now, opened=now, phase="graduated", **kw)


def test_classic_tp_sl_trail_time():
    X = A["V1_SNIPE"]["exit"]
    assert E.classic(pos(), X, 10, 1.41) == [("final", "take_profit")]
    assert E.classic(pos(), X, 10, 0.79) == [("final", "stop_loss")]
    p = pos(); E.classic(p, X, 5, 1.30)                       # peak +30% arms the 12% trail
    assert E.classic(p, X, 10, 1.30 * 0.87) == [("final", "trailing_stop")]
    assert E.classic(pos(), X, 20 * 60, 1.05) == [("final", "time_exit")]
    assert E.classic(pos(), X, 10, 1.05) == []


def test_classic_breakeven_liquidity_regime():
    X = A["C_GRAD"]["exit"]
    p = pos(cost_pct_est=2.0); E.classic(p, X, 5, 1.13)        # +13% >= breakeven_at 12%
    assert E.classic(p, X, 10, 1.005) == [("final", "breakeven_stop")]
    p = pos(entry_liq=10000, last_liq=7000)
    assert E.classic(p, X, 10, 1.0) == [("final", "liquidity_deterioration")]
    assert E.classic(pos(), X, 10, 1.0, regime_h1=-9, regime={"emergency_exit_h1_pct": -5}) == [("final", "regime_emergency_exit")]
    assert E.classic(pos(), X, 200, None) == [("final", "price_data_lost")]


def test_m4_initials_then_moonbag():
    X = A["M4_SCAN"]["exit"]
    p = pos(stage="full")
    assert E.m4(p, X, 10, 0.79) == [("final", "stop_loss")]
    p = pos(stage="full")
    assert E.m4(p, X, 10, 2.0) == [("initials", "initials_pulled")]
    E.m4_after_initials(p, X)
    assert p["stage"] == "initials_pulled" and abs(p["stop_mark"] - 1.4) < 1e-9     # 30% under 2.0, above entry
    assert E.m4(p, X, 20, 3.0) == [] and abs(p["stop_mark"] - 2.1) < 1e-9
    assert E.m4(p, X, 30, 2.05) == [("final", "moonbag_trailing_stop")]
    p = pos(stage="full"); E.m4(p, X, 1, 2.0); E.m4_after_initials(p, X)
    assert E.m4(p, X, 2, 5.0) == [("final", "moonbag_cap_5x")]
    p = pos(stage="full"); E.m4(p, X, 1, 1.2); E.m4_after_initials(p, X); p["moon_peak"] = 1.2
    assert E.m4(p, X, 2, 0.99)[0][1] == "moonbag_breakeven_stop"         # never below entry
    p = pos(stage="full")
    assert E.m4(p, X, 9 * 60, 1.1, n5=2) == [("final", "dead_token")]


def test_card_ladder_trail_timeout():
    X = A["REVIVAL"]["exit"]
    p = pos(); p["engine"] = C.Position(1.0, 0, E.card_of(X), "graduated").state()
    assert E.card(p, X, 10, 0.84, "graduated") == [("final", "stop_loss")]
    p = pos(); p["engine"] = C.Position(1.0, 0, E.card_of(X), "graduated").state()
    out = E.card(p, X, 10, 1.6, "graduated")
    assert out and out[0][0] == "partial" and abs(out[0][1] - 0.5) < 1e-9 and p["tp_done"] == 1
    p["engine"] = p["engine_next"]
    E.card(p, X, 20, 2.0, "graduated"); p["engine"] = p["engine_next"]          # peak 2.0 -> trail stop 1.6
    assert abs(p["stop_mark"] - 1.6) < 1e-9
    out = E.card(p, X, 30, 1.55, "graduated")
    assert out and out[-1] == ("final", "trailing_stop")
    p = pos(); p["engine"] = C.Position(1.0, 0, E.card_of(X), "graduated").state()
    assert E.card(p, X, 91 * 60, 1.01, "graduated")[-1][0] == "final"


def test_plan_keys_for_app():
    for k, S in A.items():
        pl = E.plan(S["exit"])
        assert pl["stop_loss_pct"] > 0 and pl["take_profit_pct"] > 0 and pl["max_hold_minutes"] > 0, k
