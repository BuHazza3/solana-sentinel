"""Adds token detail to the app state (READ-ONLY): DexScreener pair address/price per mint, entry/exit prices and exit plan
per trade (from the bot's trade records), exit plan per open position (from the config). Used by server.py, so both
the local app and the relay (GitHub Pages app) get it."""
import json, sqlite3, time
import requests

_tok, _rec = {}, {}


def _dex(mints, hot):
    now = time.time()
    need = [m for m in mints if m not in _tok or now - _tok[m][0] > (20 if m in hot else 1800)]
    for i in range(0, len(need), 30):
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


def _plan(cfg, acct, strat):
    s = dict(cfg["strategy"].get(strat, {}))
    if strat == "LS":
        s.update(cfg["accounts"].get(acct, {}).get("ls", {}))
    keys = ("take_profit_pct", "stop_loss_pct", "breakeven_at_pct", "trailing_activate_pct", "trailing_stop_pct", "max_hold_minutes", "liq_drop_exit_pct")
    return {k: s[k] for k in keys if k in s} or None


def _records(db_path, run):
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for acct, mint, closed, rec in c.execute("SELECT account, mint, closed, record FROM trades WHERE run=?", (run,)):
            k = f"x:{acct}:{mint}:{int(closed)}"
            if k not in _rec:
                try:
                    d = json.loads(rec or "{}")
                except ValueError:
                    d = {}
                _rec[k] = {"entry_mark": d.get("entry_mark"), "exit_mark": d.get("exit_mark"), "peak_mark": d.get("peak_mark"),
                           "exit_plan": d.get("exit_plan"), "rug_score": d.get("rug_score"), "entry_liq": d.get("entry_liq"), "checks": d.get("checks")}
        c.close()
    except Exception:
        pass


def enrich(state, cfg, db_path):
    if not state or not state.get("accounts"):
        return state
    _records(db_path, state.get("run"))
    hot, mints = set(), set()
    for a in state["accounts"].values():
        for h in a.get("holdings", []):
            hot.add(h["mint"]); mints.add(h["mint"])
        for x in a.get("closes", []) + a.get("entries", []):
            mints.add(x["mint"])
    _dex(sorted(mints, key=lambda m: m not in hot), hot)
    tokens = {}
    for m in mints:
        v = _tok.get(m, (0, None))[1]
        if v:
            tokens[m] = v
    for name, a in state["accounts"].items():
        for h in a.get("holdings", []):
            h["id"] = f"h:{name}:{h['mint']}:{int(h['opened'])}"
            h["pair"] = (tokens.get(h["mint"]) or {}).get("pair")
            if not h.get("exit_plan"):
                h["exit_plan"] = _plan(cfg, name, h["strategy"])
            raw = h.pop("tokens_raw", None)
            if h.get("entry_mark") is None:
                h["entry_mark"] = h["size_sol"] / (raw / 1e6) if raw and h.get("size_sol") else None
        for x in a.get("closes", []):
            x["pair"] = (tokens.get(x["mint"]) or {}).get("pair")
            x.update({k: v for k, v in (_rec.get(x["id"]) or {}).items() if v is not None and k != "checks"})
            if not x.get("exit_plan"):
                x["exit_plan"] = _plan(cfg, name, x["strategy"])
        for e in a.get("entries", []):
            e["pair"] = (tokens.get(e["mint"]) or {}).get("pair")
        a["plans"] = dict({s: _plan(cfg, name, s) for s in ("A", "B", "C", "LS", "V1")}, **(a.get("plans") or {}))
        for p in a.get("partials", []):
            p["pair"] = (tokens.get(p["mint"]) or {}).get("pair")
    state["tokens"] = tokens
    return state
