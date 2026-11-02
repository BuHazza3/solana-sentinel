"""Tests for the S5 live helpers and the Revival rule (written before s5_quant.py)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import s5_core as C, s5_signals as S

T0 = 1_800_000_000 - (1_800_000_000 % 60)


def series(n_quiet=20, peak_px=None, wake_px=None, wake_vol=5.0):
    """Token created 60 min ago: an early spike (peak), then a quiet 20 min, then a wake candle."""
    p_peak = peak_px or C.price_at_progress(40)
    p_low = C.price_at_progress(10)
    cs = []
    t = T0
    cs.append((t, p_low, p_peak, p_low, p_low * 1.2, 30.0, "bonding")); t += 60      # spike & dump
    for k in range(n_quiet):
        cs.append((t, p_low, p_low, p_low, p_low, 0.02, "bonding")); t += 60
    pw = wake_px or C.price_at_progress(16)                                               # ~+5 SOL into the curve
    cs.append((t, p_low, pw, p_low, pw, wake_vol, "bonding"))
    return {"c": cs, "created": T0 - 40 * 60, "seen": 0, "grad": None}


def test_revival_fires_on_wake():
    tok = series()
    assert list(S.sig_revival(tok)) == [len(tok["c"]) - 1]


def test_revival_needs_quiet():
    tok = series()
    tok["c"][-3] = tok["c"][-3][:5] + (3.0, "bonding")          # 3 SOL traded 2 min ago -> not quiet
    assert list(S.sig_revival(tok)) == []


def test_revival_needs_inflow_not_just_volume():
    tok = series(wake_px=C.price_at_progress(10.5))              # big volume but price barely moved: no net buying
    assert list(S.sig_revival(tok)) == []


def test_revival_needs_far_below_peak():
    tok = series(peak_px=C.price_at_progress(18))                 # wake price is not <= 50% of the peak
    assert list(S.sig_revival(tok)) == []


def test_revival_min_age():
    tok = series()
    tok["created"] = tok["c"][-1][0] - 10 * 60
    assert list(S.sig_revival(tok)) == []


def test_fires_now_uses_closed_candles_only():
    tok = series()
    last = tok["c"][-1][0]
    assert S.fires_now(S.sig_revival, tok, now=last + 61)
    assert not S.fires_now(S.sig_revival, tok, now=last + 30)     # wake minute still open
    assert not S.fires_now(S.sig_revival, tok, now=last + 400)    # stale


def test_jup_to_tok_converts_to_sol():
    raw = [{"time": T0, "open": 1e-5, "high": 2e-5, "low": 1e-5, "close": 1.5e-5, "volume": 900}]
    t = S.jup_to_tok(raw, 90.0, T0 - 60, graduated=False)
    assert abs(t["c"][0][4] - 1.5e-5 / 90) < 1e-15 and abs(t["c"][0][5] - 10) < 1e-9 and t["c"][0][6] == "bonding"
