"""Sentinel app server (READ-ONLY view of the V1 and S5 multi-agent paper bots). Token-protected; no trade, wallet or deposit actions.
On/off switches are NOT handled here: they arrive as encrypted, authenticated commands through relay_publisher.py (toggle_cmd.py)."""
import asyncio, json, os, re, threading, time
from datetime import timedelta, timezone
import requests
from enrich import enrich
import bots_view
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse, PlainTextResponse

BOT = os.environ.get("BOT_DIR", "/workspace/solana-bot")
APP = os.path.dirname(os.path.abspath(__file__))
TOKEN = open(os.path.join(APP, ".token")).read().strip()
DUBAI = timezone(timedelta(hours=4))
STALE_S = 120
_cache = {}

app =FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def auth(request: Request, call_next):
    p = request.url.path
    public = p == "/sw.js" or p.startswith("/icons/")  # generic static assets only, no bot data
    if not public and request.query_params.get("t") != TOKEN:
        return PlainTextResponse("Forbidden", status_code=403)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def cached(key, ttl, fn):
    v = _cache.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    try:
        r = fn()
    except Exception:
        r = v[1] if v else None
    _cache[key] = (time.time(), r)
    return r


def token_info(mints):
    """DexScreener token images (cached 30 min per batch)."""
    out = {}
    need = [m for m in mints if f"img:{m}" not in _cache]
    for i in range(0, len(need), 30):
        batch = need[i:i + 30]
        try:
            r = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch), timeout=8).json()
            got = {}
            for p in r or []:
                for side in ("baseToken",):
                    m = p[side]["address"]
                    img = (p.get("info") or {}).get("imageUrl")
                    if m in batch and img and m not in got:
                        got[m] = img
            for m in batch:
                _cache[f"img:{m}"] = (time.time(), got.get(m))
        except Exception:
            pass
    for m in mints:
        v = _cache.get(f"img:{m}")
        out[m] = v[1] if v else None
    return out


# ------------------------------------------------------------------ plain-word reasons
def plain(reason):
    r = reason
    rules = [
        (r"^F_regime_cash:(.*)", lambda m: f"market check said stay in cash ({m[1]})"),
        (r"^risk_breaker:(.*)", lambda m: f"risk breaker on ({m[1].replace('_', ' ')})"),
        (r"^already_traded", lambda m: "already traded this token (no averaging)"),
        (r"^max_concurrent_positions", lambda m: "max open positions reached"),
        (r"^ls_slot_full", lambda m: "sniper slot already in use"),
        (r"^ls_tier_capital_cap", lambda m: "sniper tier capital cap"),
        (r"^liquidity_below_min\((\d+)<(\d+)\)", lambda m: f"liquidity ${int(m[1]):,} below ${int(m[2]):,}"),
        (r"^stale_market_data", lambda m: "market data stale"),
        (r"^security_data_unavailable", lambda m: "no security report"),
        (r"^entry_chased\(m5_pump\)", lambda m: "price already pumped in last 5 min"),
        (r"^veto:(.*)", lambda m: "safety veto: " + m[1].replace("_", " ")),
        (r"^rug_score\((\d+)\+?>(\d+)\)", lambda m: f"rug risk {m[1]} > {m[2]}"),
        (r"^switched_off:(.*)", lambda m: f"switched off ({m[1]})"),
        (r"^risk_gate:(.*)", lambda m: "risk limit: " + m[1].replace("_", " ")),
        (r"^already_holding", lambda m: "already holding this token"),
        (r"^rr_after_costs\(([\d.]+)<([\d.]+)\)", lambda m: f"reward/risk {m[1]} < {m[2]} after costs"),
    ]
    for pat, fn in rules:
        m = re.match(pat, r)
        if m:
            return fn(m)
    return r.replace("_", " ")



def runner_status():
    f = os.environ.get("RUNNER_STATUS")
    if not f or not os.path.exists(f):
        return None
    try:
        return json.load(open(f))
    except Exception:
        return None


def sol_at(regs, ts, fallback):
    best = None
    for t, p in regs:
        if t <= ts + 60:
            best = p
        else:
            break
    return best or fallback


def phase_split(closes):
    out = {}
    for ph in ("bonding", "graduated"):
        t = [x for x in closes if x.get("phase") == ph]
        out[ph] = {"trades": len(t), "wins": sum(1 for x in t if x["pnl_sol"] > 0), "pnl_sol": sum(x["pnl_sol"] for x in t),
                   "win_rate": sum(1 for x in t if x["pnl_sol"] > 0) / len(t) * 100 if t else None}
    return out


def sol_price():
    def f():
        r = requests.get("https://lite-api.jup.ag/price/v3", params={"ids": "So11111111111111111111111111111111111111112"}, timeout=8).json()
        return float(r["So11111111111111111111111111111111111111112"]["usdPrice"])
    return cached("solusd", 60, f)


def relay_cmd_status():
    try:
        return json.load(open(os.path.join(APP, "logs", "relay_status.json")))
    except Exception:
        return None


RETIRED = {"REVISED": "Latest rules (REVISED)", "ORIGINAL": "Original", "M4": "M4 Momentum"}


def compat_v6(accounts):
    """The GitHub Pages app is v6 until the v7 page is published. v6 has fixed tabs (REVISED / ORIGINAL / V1_SNIPER / M4 / S5)
    and its render() throws on a missing account, which left the previous tab's holdings on screen ("all strategies show the
    same holdings"). So: V1_SNIPER -> the V1 account (its continuation), and each retired tab gets an EMPTY placeholder with
    its final paper result, so every tab shows only its own positions. v7 drops these keys and shows only V1 and S5."""
    if "V1" in accounts:
        accounts["V1_SNIPER"] = accounts["V1"]
    L = bots_view.legacy() or {}
    for k, title in RETIRED.items():
        net = sum(r["net"] for r in L.get("rows", []) if r["bot"] == title)
        rows = [r for r in L.get("rows", []) if r["bot"] == title]
        accounts[k] = {"retired": True, "title": title, "equity": 1.0 + net, "start": 1.0, "open": 0, "closed_n": 0, "halt": "retired",
                       "series": [], "closes": [], "holdings": [], "entries": [], "partials": [], "phases": None, "plans": {},
                       "stats": {"trades": 0, "win_rate": None, "best": None, "pnl_sol": 0.0}, "copy_signals": 0, "alive": False,
                       "activity": [{"ts": time.time(), "type": "skip", "title": f"{title} was retired",
                                     "sub": "Its winning sub-agents now run in V1 Sniper and S5 Quant. Reopen the app to update it (v7).", "mint": ""}],
                       "agents": [{"key": "RISK", "title": f"{title}: retired", "used": False, "desc": "No positions. Results of all its runs: "
                                   + "; ".join(f"{r['agent']} {r['wins']}W/{r['losses']}L {r['net']:+.4f} SOL" for r in rows),
                                   "task": "retired 2 Nov 2026 19:19 (Dubai)", "last": None, "metrics": [["Net SOL", f"{net:+.4f}"], ["Open", "0"], ["Status", "retired"]]}]}


def build_state():
    now = time.time()
    accounts, errs = {}, {}
    sol_usd = None
    for b in ("V1", "S5"):
        try:
            a = bots_view.build(b, sol_at, [], sol_usd or sol_price(), token_info, plain, phase_split)
            if a:
                accounts[b] = a
        except Exception as e:
            errs[b] = repr(e)
    STATE["errs"] = errs
    sol_usd = sol_price()
    alive = any(a["alive"] for a in accounts.values())
    started = min([a["started"] for a in accounts.values()] or [now])
    mt = 0
    for b in ("v1", "s5"):
        try:
            mt = max(mt, os.path.getmtime(os.path.join(BOT, "data", f"{b}_bot_state.json")))
        except OSError:
            pass
    cyc = (accounts.get("V1") or accounts.get("S5") or {}).get("cycles") or {}
    dex = cyc.get("dex") or {}
    rs = relay_cmd_status() or {}
    out = {"runner": runner_status(), "run": " · ".join(f"{k}:{v['run']}" for k, v in accounts.items()), "now": now, "log_mtime": mt or None,
            "fresh": bool(mt) and now - mt < STALE_S, "bot_alive": alive, "sol_usd": sol_usd, "started": started,
            "last_cycle": {"found": dex.get("found"), "evaluated": dex.get("evaluated"), "regime": dex.get("regime")} if dex else None,
            "last_cycle_ts": dex.get("ts"), "accounts": accounts, "rules": bots_view.rules() + [
                {"title": "Copied wallets", "lines": [f"{'✓ VERIFIED' if w['verified'] else 'UNVERIFIED'} · {w['label']}: {w['evidence']}" for w in bots_view.wallets()]},
                {"title": "Paper mode", "lines": ["Simulated trades only: real live prices and real Jupiter quotes, no wallet, no keys, no transactions."]}],
            "mode": "multi", "wallets": bots_view.wallets(), "legacy": bots_view.legacy(),
            "switch_channel": {"subscribed": bool(rs.get("commands_subscribed")), "applied": rs.get("commands_applied", 0), "rejected": rs.get("commands_rejected", 0)}}
    compat_v6(out["accounts"])   # added last, so the placeholders never affect the V1/S5 summaries above
    return out


STATE = {"data": None, "ver": 0}


def refresher():
    while True:
        try:
            STATE["data"] = enrich(build_state())
            STATE["ver"] += 1
        except Exception as e:
            STATE["err"] = repr(e)
        time.sleep(2.5)


threading.Thread(target=refresher, daemon=True).start()


@app.get("/")
def index():
    html = open(os.path.join(APP, "static", "index.html")).read().replace("__TOKEN__", TOKEN)
    return HTMLResponse(html)


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "Solana Sentinel", "short_name": "Sentinel", "id": "/?app=sentinel",
        "start_url": f"/?t={TOKEN}", "scope": "/", "display": "standalone", "orientation": "portrait",
        "background_color": "#07060b", "theme_color": "#07060b", "description": "Live paper-trading view of the Sentinel Solana bot",
        "icons": [{"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]},
        media_type="application/manifest+json")


@app.get("/sw.js")
def sw():
    return FileResponse(os.path.join(APP, "static", "sw.js"), media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/icons/{name}")
def icons(name: str):
    p = os.path.join(APP, "static", "icons", os.path.basename(name))
    return FileResponse(p) if os.path.exists(p) else PlainTextResponse("not found", status_code=404)


@app.get("/api/state")
def api_state():
    return JSONResponse(STATE["data"] or {"loading": True, "err": STATE.get("err")})


@app.get("/api/stream")
async def stream(request: Request):
    async def gen():
        last = -1
        while True:
            if await request.is_disconnected():
                break
            if STATE["ver"] != last and STATE["data"]:
                last = STATE["ver"]
                yield f"data: {json.dumps(STATE['data'])}\n\n"
            else:
                yield ": ping\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
