"""Adds DexScreener pair address / price per mint to the app state (READ-ONLY). Used by server.py, so both the local app and
the relay (GitHub Pages app) get it. Trade records and exit plans now come straight from the bots' own databases (bots_view.py)."""
import time
import requests

_tok = {}


def _dex(mints, hot):
    now = time.time()
    need = [m for m in mints if m not in _tok or now - _tok[m][0] > (20 if m in hot else 1800)]
    for i in range(0, min(len(need), 300), 30):
        batch = need[i:i + 30]
        try:
            r = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch), timeout=8).json() or []
        except Exception:
            continue
        best = {}
        for p in r:
            m = (p.get("baseToken") or {}).get("address")
            if m in batch and (m not in best or ((p.get("liquidity") or {}).get("usd") or 0) > ((best[m].get("liquidity") or {}).get("usd") or 0)):
                best[m] = p
        for m in batch:
            p = best.get(m)
            v = None
            if p:
                v = {"pair": p.get("pairAddress"), "dex": p.get("dexId"), "name": (p.get("baseToken") or {}).get("name"),
                     "pu": float(p["priceUsd"]) if p.get("priceUsd") else None, "pn": float(p["priceNative"]) if p.get("priceNative") else None,
                     "liq": (p.get("liquidity") or {}).get("usd"), "mc": p.get("marketCap") or p.get("fdv"),
                     "m5": (p.get("priceChange") or {}).get("m5"), "h1": (p.get("priceChange") or {}).get("h1"), "h24": (p.get("priceChange") or {}).get("h24")}
            _tok[m] = (now, v if v else (_tok.get(m, (0, None))[1]))


def enrich(state):
    if not state or not state.get("accounts"):
        return state
    hot, mints = set(), set()
    for a in state["accounts"].values():
        for h in a.get("holdings", []):
            hot.add(h["mint"]); mints.add(h["mint"])
        for x in a.get("closes", [])[:150] + a.get("entries", [])[:150]:
            mints.add(x["mint"])
    _dex(sorted(mints, key=lambda m: m not in hot), hot)
    tokens = {m: _tok[m][1] for m in mints if m in _tok and _tok[m][1]}
    for a in state["accounts"].values():
        for x in a.get("holdings", []) + a.get("closes", []) + a.get("entries", []) + a.get("partials", []):
            x["pair"] = (tokens.get(x["mint"]) or {}).get("pair")
    state["tokens"] = tokens
    return state
