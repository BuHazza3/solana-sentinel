"""Tests for s5_core (written before the implementation). Run: python3 -m pytest -q tests/test_s5_core.py"""
import math, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import s5_core as C


# ---------- pump.fun curve math
def test_curve_progress_bounds():
    p0 = C.CURVE_V_SOL0 / C.CURVE_V_TOK0
    assert abs(C.curve_progress(p0)) < 1e-6
    assert abs(C.curve_progress(C.GRAD_PRICE_SOL) - 100) < 1e-6
    assert 40 < C.curve_progress(C.price_at_progress(50)) < 60
    assert C.curve_progress(p0 / 2) == 0          # clamped


def test_virtual_sol_matches_constant_product():
    p = C.price_at_progress(30)
    v = C.curve_virtual_sol(p)
    assert abs(v - (30 + 0.30 * C.CURVE_REAL_SOL_GRAD)) < 1e-6


# ---------- costs
def test_fee_pct_tiers():
    assert C.pool_fee_pct("bonding", 100) == 1.25
    assert C.pool_fee_pct("graduated", 500) == 1.20
    assert C.pool_fee_pct("graduated", 60000) == 0.55


def test_buy_cost_includes_fee_impact_latency():
    px = C.price_at_progress(50)
    fill = C.buy_fill_price(px, 0.1, "bonding")
    assert fill > px * 1.0125 * 1.01          # fee + latency at least
    big = C.buy_fill_price(px, 5.0, "bonding")
    assert big > fill                          # size moves price (impact)


def test_sell_cost_symmetric():
    px = C.price_at_progress(50)
    out = C.sell_fill_price(px, 0.1, "bonding")
    assert out < px * (1 - 0.0125) * (1 - 0.01)


def test_round_trip_flat_price_loses_costs():
    px = C.price_at_progress(40)
    size = 0.05
    tokens = size / C.buy_fill_price(px, size, "bonding")
    back = tokens * C.sell_fill_price(px, size, "bonding") - 2 * C.TX_FEE_SOL
    loss = 1 - back / size
    assert 0.04 < loss < 0.10                  # ~2.5% fees + 2% latency + impact + network fees


# ---------- sizing / risk
def test_size_one_percent_risk():
    assert abs(C.position_size(1.0, 0.20, cap_frac=0.5) - 0.05) < 1e-12
    assert abs(C.position_size(1.0, 0.05, cap_frac=0.10) - 0.10) < 1e-12   # capped
    assert C.position_size(1.0, None, cap_frac=0.5) == 0.01               # no stop: whole stake is the risk


def test_risk_gate():
    r = C.RiskGate(start_equity=1.0, max_open=3, daily_stop_pct=3.0, tz_offset_h=4)
    t = 1_800_000_000
    assert r.can_enter(t, open_n=2, equity=1.0)[0]
    assert not r.can_enter(t, open_n=3, equity=1.0)[0]
    r.day_start(t, 1.0)
    assert not r.can_enter(t + 60, open_n=0, equity=0.969)[0]           # -3.1% today
    assert r.can_enter(t + 86400, open_n=0, equity=0.969)[0]             # next day resets


# ---------- exit engine
CARD = dict(stop_pct=20, tp=[(1.4, 0.5)], trail_pct=None, trail_arm_x=None, timeout_min=30, close_below_entry=False,
            exit_on_graduation=False, exit_after_grad_min=None)


def mk(card=CARD, entry=1.0, ts=0, phase="bonding"):
    return C.Position(entry_px=entry, opened=ts, card=card, phase=phase)


def test_stop_at_stop_price_when_close_above():
    p = mk()
    acts = p.step(60, 1.0, 1.0, 0.75, 0.9, "bonding")
    assert acts == [(1.0, 0.8, "stop_loss")]


def test_stop_fills_at_close_when_close_below():
    p = mk()
    acts = p.step(60, 0.95, 0.95, 0.3, 0.4, "bonding")
    assert acts == [(1.0, 0.4, "stop_loss")]


def test_gap_open_below_stop_fills_at_open_or_worse():
    p = mk()
    acts = p.step(60, 0.5, 0.6, 0.45, 0.55, "bonding")
    assert acts[0][1] == 0.5 and acts[0][2] == "stop_loss"


def test_tp_partial_then_timeout():
    p = mk()
    a1 = p.step(60, 1.0, 1.5, 0.95, 1.45, "bonding")
    assert a1 == [(0.5, 1.4, "take_profit_1.4x")]
    assert abs(p.left - 0.5) < 1e-12
    a2 = p.step(31 * 60, 1.3, 1.3, 1.2, 1.25, "bonding")
    assert a2 == [(0.5, 1.3, "time_exit")]
    assert p.closed


def test_stop_beats_tp_in_same_candle():
    p = mk()
    acts = p.step(60, 1.0, 1.6, 0.7, 1.0, "bonding")
    assert acts[0][2] == "stop_loss"


def test_trailing_stop():
    card = dict(CARD, tp=[], trail_pct=15, trail_arm_x=1.0, stop_pct=25, timeout_min=None)
    p = mk(card)
    assert p.step(60, 1.0, 1.5, 1.0, 1.5, "bonding") == []
    acts = p.step(120, 1.45, 1.45, 1.2, 1.25, "bonding")
    assert acts == [(1.0, 1.25, "trailing_stop")]   # closed below the trail -> conservative fill at the close


def test_close_below_entry_rule():
    card = dict(CARD, stop_pct=None, close_below_entry=True)
    p = mk(card)
    assert p.step(60, 1.0, 1.1, 0.9, 1.01, "bonding") == []
    assert p.step(120, 1.0, 1.0, 0.9, 0.95, "bonding") == [(1.0, 0.95, "close_below_entry")]


def test_exit_on_graduation():
    card = dict(CARD, stop_pct=12, tp=[], exit_on_graduation=True, timeout_min=20)
    p = mk(card)
    assert p.step(60, 1.0, 1.05, 0.95, 1.04, "bonding") == []
    acts = p.step(120, 1.3, 1.4, 1.2, 1.35, "graduated")
    assert acts == [(1.0, 1.3, "graduated_sold")]


def test_exit_after_grad_minutes():
    card = dict(CARD, stop_pct=20, tp=[], exit_after_grad_min=3, timeout_min=None)
    p = mk(card, ts=0, phase="graduated")
    p.grad_ts = 0
    assert p.step(60, 1.0, 1.1, 0.95, 1.05, "graduated") == []
    assert p.step(180, 1.02, 1.1, 1.0, 1.08, "graduated") == [(1.0, 1.02, "time_after_graduation")]


def test_mark_only_tick_works_for_live():
    p = mk()
    assert p.tick(10, 0.79, "bonding") == [(1.0, 0.79, "stop_loss")]
