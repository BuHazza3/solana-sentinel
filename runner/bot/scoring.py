"""Momentum/quality score in [0,1] from DexScreener pair data."""
import math, time


def age_minutes(pair):
    t = pair.get("_first_created_ms") or pair.get("pairCreatedAt")
    return (time.time() * 1000 - t) / 60000 if t else None


def score(pair, info=None):
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    vol_h1 = (pair.get("volume") or {}).get("h1") or 0
    tx = pair.get("txns") or {}
    b5, s5 = (tx.get("m5") or {}).get("buys", 0), (tx.get("m5") or {}).get("sells", 0)
    b1, s1 = (tx.get("h1") or {}).get("buys", 0), (tx.get("h1") or {}).get("sells", 0)
    pc = pair.get("priceChange") or {}
    s_liq = min(1, max(0, math.log10(max(liq, 1) / 20000) / 1.0))           # 20k->0, 200k->1
    s_vol = min(1, (vol_h1 / liq) / 2) if liq else 0                           # h1 volume = 2x liq -> 1
    s_bs = min(1, max(0, ((b5 + b1 / 12) / max(1, s5 + s1 / 12) - 0.8) / 1.2))  # buy/sell 0.8->0, 2.0->1
    s_act = min(1, (b5 + s5) / 60)                                             # 60 tx in 5m -> 1
    m5, h1 = pc.get("m5") or 0, pc.get("h1") or 0
    s_mom = 1.0 if 0 < m5 < 25 and h1 > 0 else 0.5 if m5 > 0 else 0.0
    if h1 > 300 or m5 > 40:   # parabolic -> late
        s_mom *= 0.3
    a = age_minutes(pair)
    s_age = 1.0 if a is not None and a < 24 * 60 else 0.5
    s_hold = 1.0
    if info and isinstance(info.get("top10_pct"), (int, float)):
        s_hold = max(0, 1 - info["top10_pct"] / 40)
    w = dict(liq=.2, vol=.2, bs=.2, act=.1, mom=.15, age=.05, hold=.1)
    total = (w["liq"] * s_liq + w["vol"] * s_vol + w["bs"] * s_bs + w["act"] * s_act + w["mom"] * s_mom
             + w["age"] * s_age + w["hold"] * s_hold)
    return round(total, 3), dict(liq=round(s_liq, 2), vol=round(s_vol, 2), bs=round(s_bs, 2), act=round(s_act, 2),
                                 mom=s_mom, age=s_age, hold=round(s_hold, 2), m5=m5, h1=h1, b5=b5, s5=s5)
