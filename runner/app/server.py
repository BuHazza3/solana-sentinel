#!/usr/bin/env python3
"""Solana Sentinel live app backend. READ-ONLY over /workspace/solana-bot data (never writes there).
Serves a single-page app + /api/state + /api/stream (SSE). Every request must carry ?t=<ACCESS_TOKEN>."""
import asyncio, glob, json, os, re, sqlite3, subprocess, threading, time, tomllib
from datetime import datetime, timedelta, timezone
import requests
from enrich import enrich
import m4_view
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse, PlainTextResponse

BOT = os.environ.get("BOT_DIR", "/workspace/solana-bot")
APP = os.path.dirname(os.path.abspath(__file__))
TOKEN = open(os.path.join(APP, ".token")).read().strip()
DUBAI = timezone(timedelta(hours=4))
JUP = "https://lite-api.jup.ag"
WSOL = "So11111111111111111111111111111111111111112"
MODE = os.environ.get("BOT_MODE", "pump")  # "pump" = pump_sentinel.py (pump.fun only), "all" = sentinel.py
PFX, RUNL, DBF, CFGF = (("pump_sentinel_", "P", "pump_sentinel.db", "pump_sentinel.toml") if MODE == "pump" else ("sentinel_", "S", "sentinel.db", "sentinel.toml"))
CFG = tomllib.load(open(os.path.join(BOT, CFGF), "rb"))
G = CFG["general"]
LAT = G["latency_slippage_bps"] / 1e4
STALE_S = 120

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


# ------------------------------------------------------------------ app's own store (log history survives log rotation)
adb = sqlite3.connect(os.path.join(APP, "app.db"), check_same_thread=False)
adb.executescript("""CREATE TABLE IF NOT EXISTS ev (run TEXT, ts REAL, kind TEXT, data TEXT, UNIQUE(run, ts, kind, data));
CREATE TABLE IF NOT EXISTS off (path TEXT PRIMARY KEY, ino INTEGER, pos INTEGER, last_ts REAL, day TEXT);""")
ALOCK = threading.Lock()

STATUS_RE = re.compile(r"(\w+): eq=([\d.]+) open=(\d+) closed=(\d+)( HALT:\S+)?")
LINE_RE = re.compile(r"^(\d\d):(\d\d):(\d\d) (\S+)\s+(.*)$")


def current_log():
    logs = [p for p in glob.glob(os.path.join(BOT, "data", f"{PFX}{RUNL}*.log")) if re.search(rf"/{PFX}{RUNL}\d{{8}}-\d{{6}}\.log$", p)]
    return max(logs, key=os.path.getmtime) if logs else None


def run_of(path):
    return re.search(r"([SP]\d{8}-\d{6})\.log", path).group(1)


def ingest(path):
    """Incrementally parse the bot log into app.db (handles copytruncate rotation)."""
    run = run_of(path)
    st = os.stat(path)
    with ALOCK:
        row = adb.execute("SELECT ino, pos, last_ts, day FROM off WHERE path=?", (path,)).fetchone()
        if row and row[0] == st.st_ino and row[1] <= st.st_size:
            pos, last_ts, day = row[1], row[2], row[3]
        else:
            pos, last_ts, day = 0, 0, None
            if row and row[0] == st.st_ino and row[1] > st.st_size:  # truncated in place: keep date context
                last_ts, day = row[2], row[3]
        if day is None:
            day = datetime.strptime(run[1:9], "%Y%m%d").strftime("%Y-%m-%d")
        with open(path, "rb") as f:
            f.seek(pos)
            chunk = f.read()
        end = chunk.rfind(b"\n") + 1
        lines = chunk[:end].decode("utf-8", "replace").splitlines()
        pos += end
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=DUBAI)
        for ln in lines:
            m = LINE_RE.match(ln)
            if not m:
                continue
            hh, mm, ss, name, msg = int(m[1]), int(m[2]), int(m[3]), m[4], m[5]
            ts = (d0 + timedelta(hours=hh, minutes=mm, seconds=ss)).timestamp()
            while last_ts and ts < last_ts - 3600:  # midnight rollover
                d0 += timedelta(days=1); day = d0.strftime("%Y-%m-%d")
                ts = (d0 + timedelta(hours=hh, minutes=mm, seconds=ss)).timestamp()
            last_ts = max(last_ts or 0, ts)
            kind, data = None, None
            if msg.startswith("STATUS "):
                kind = "status"
                data = {a: {"eq": float(e), "open": int(o), "closed": int(c), "halt": (h or "").replace(" HALT:", "") or None}
                        for a, e, o, c, h in STATUS_RE.findall(msg)}
            elif msg.startswith("cycle:"):
                mm_ = re.search(r"(\d+) (?:found|pump candidates), (\d+) evaluated, regime=(\S+) SOL=\$([\d.]+)", msg)
                if mm_:
                    kind, data = "cycle", {"found": int(mm_[1]), "evaluated": int(mm_[2]), "regime": mm_[3], "sol": float(mm_[4])}
            elif msg.startswith("COPY signal:"):
                mm_ = re.search(r"COPY signal: (\S+) bought (\S+) \(~([\d.]+) SOL\)", msg)
                if mm_:
                    kind, data = "copy", {"wallet": mm_[1], "mint": mm_[2], "sol": float(mm_[3])}
            elif msg.startswith("leader "):
                mm_ = re.search(r"leader (\S+) bought (.+?) \((\w{8})\)(?: \[(\w+)\])? ~([\d.]+) SOL liq=\$(\d+); wallets in window=\[(.*)\]", msg)
                if mm_:
                    kind, data = "leader", {"wallet": mm_[1], "symbol": mm_[2], "mint8": mm_[3], "phase": mm_[4], "sol": float(mm_[5]), "liq": int(mm_[6]),
                                            "window": re.findall(r"'([^']+)'", mm_[7])}
                else:
                    mm_ = re.search(r"leader (\S+) bought (\S+): no DEX pair data", msg)
                    if mm_:
                        kind, data = "leader", {"wallet": mm_[1], "mint": mm_[2], "symbol": None, "nopair": True}
            elif msg.startswith("SENTINEL paper run"):
                kind, data = "start", {"msg": msg}
            if kind:
                adb.execute("INSERT OR IGNORE INTO ev VALUES (?,?,?,?)", (run, ts, kind, json.dumps(data, sort_keys=True)))
        adb.execute("INSERT OR REPLACE INTO off VALUES (?,?,?,?,?)", (path, st.st_ino, pos, last_ts, day))
        adb.commit()
    return run, st.st_mtime


def evs(run, kind, since=0):
    with ALOCK:
        return [(ts, json.loads(d)) for ts, d in adb.execute("SELECT ts, data FROM ev WHERE run=? AND kind=? AND ts>=? ORDER BY ts", (run, kind, since))]


# ------------------------------------------------------------------ external lookups (cached)
_cache = {}


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


def sell_quote(mint, raw):
    def f():
        r = requests.get(f"{JUP}/swap/v1/quote", params={"inputMint": mint, "outputMint": WSOL, "amount": int(raw),
                         "slippageBps": G["max_slippage_bps"], "restrictIntermediateTokens": "true"}, timeout=8).json()
        return int(r["outAmount"]) / 1e9 * (1 - LAT) if r.get("outAmount") else None
    return cached(f"q:{mint}:{int(raw)}", 10, f)


def fee_of(strat):
    f = CFG["fees"][CFG["strategy"][strat]["fees"]]
    return f["base_fee_sol"] + f["priority_fee_sol"] + f["jito_tip_sol"]


def bot_alive():
    try:
        out = subprocess.run(["pgrep", "-af", "sentinel.py"], capture_output=True, text=True).stdout
        name = "pump_sentinel.py" if MODE == "pump" else "sentinel.py"
        return any("python" in l and re.search(r"(^|/| )" + re.escape(name) + r"( |$)", l) and "supervisor" not in l for l in out.splitlines())
    except Exception:
        return False


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
        (r"^rr_after_costs\(([\d.]+)<([\d.]+)\)", lambda m: f"reward/risk {m[1]} < {m[2]} after costs"),
    ]
    for pat, fn in rules:
        m = re.match(pat, r)
        if m:
            return fn(m)
    return r.replace("_", " ")


# ------------------------------------------------------------------ runner (GitHub Actions) health, written by runner.py
def runner_status():
    f = os.environ.get("RUNNER_STATUS")
    if not f or not os.path.exists(f):
        return None
    try:
        return json.load(open(f))
    except Exception:
        return None


# ------------------------------------------------------------------ state
STRAT_NAME = {"A": "Copy wallet", "B": "Multi-whale", "C": "Momentum", "LS": "Launch sniper", "V1": "V1 sniper"}


def bdb():
    return sqlite3.connect(f"file:{os.path.join(BOT, 'data', DBF)}?mode=ro", uri=True, timeout=5)


def sol_at(regs, ts, fallback):
    best = None
    for t, p in regs:
        if t <= ts + 60:
            best = p
        else:
            break
    return best or fallback


def phase_split(closes):
    if MODE != "pump":
        return None
    out = {}
    for ph in ("bonding", "graduated"):
        t = [x for x in closes if x.get("phase") == ph]
        out[ph] = {"trades": len(t), "wins": sum(1 for x in t if x["pnl_sol"] > 0), "pnl_sol": sum(x["pnl_sol"] for x in t),
                   "win_rate": sum(1 for x in t if x["pnl_sol"] > 0) / len(t) * 100 if t else None}
    return out


def wallets():
    return [{"label": w["label"], "address": w["address"], "verified": w.get("evidence", "").lower().startswith(("verified", "on-chain")),
             "evidence": re.sub(r"^(verified on-chain|unverified)[^:]*:\s*", "", w.get("evidence", ""))} for w in CFG["wallets"]["follow"]]


def build_state():
    path = current_log()
    if not path:
        return {"error": "no bot log found"}
    run, mtime = ingest(path)
    now = time.time()
    c = bdb()
    regs = c.execute("SELECT ts, sol_usd, m5, h1, h6, h24, ok, why FROM regime WHERE run=? ORDER BY ts", (run,)).fetchall()
    cycles = evs(run, "cycle")
    sol_usd = (cycles[-1][1]["sol"] if cycles else None) or (regs[-1][1] if regs else None)
    reg_pts = [(r[0], r[1]) for r in regs]
    statuses = evs(run, "status")
    PH = "phase" if MODE == "pump" else "NULL"
    trades = c.execute(f"SELECT account, strategy, mint, symbol, opened, closed, size_sol, sol_out, fees_sol, pnl_sol, pnl_pct, exit_reason, {PH} "
                       "FROM trades WHERE run=? ORDER BY closed", (run,)).fetchall()
    approved = c.execute(f"SELECT ts, account, strategy, mint, symbol, detail, {PH} FROM decisions WHERE run=? AND approved=1 ORDER BY ts", (run,)).fetchall()
    dec_counts = c.execute("SELECT account, strategy, COUNT(*), SUM(approved), MAX(ts) FROM decisions WHERE run=? GROUP BY account, strategy", (run,)).fetchall()
    last_dec = {}
    for acct, strat in [(a, s) for a, s, *_ in dec_counts]:
        r = c.execute("SELECT ts, symbol, approved, reasons FROM decisions WHERE run=? AND account=? AND strategy=? ORDER BY ts DESC LIMIT 1",
                      (run, acct, strat)).fetchone()
        last_dec[(acct, strat)] = r
    rej = c.execute("SELECT ts, account, strategy, mint, symbol, reasons FROM decisions WHERE run=? AND approved=0 AND strategy IN ('A','B','C','LS','V1') "
                    "ORDER BY ts DESC LIMIT 400", (run,)).fetchall()
    reason_counts = {}
    for acct, reasons in c.execute("SELECT account, reasons FROM decisions WHERE run=? AND approved=0", (run,)):
        for r in reasons.split(";"):
            k = re.split(r"[(:]", r)[0]
            reason_counts.setdefault(acct, {}).setdefault(k, 0)
            reason_counts[acct][k] += 1
    scan_n, scan_veto = c.execute("SELECT COUNT(*), SUM(vetoes!='') FROM scans WHERE run=?", (run,)).fetchone()
    last_scan = c.execute("SELECT ts, symbol, rug_score, vetoes FROM scans WHERE run=? ORDER BY ts DESC LIMIT 1", (run,)).fetchone()
    c.close()

    closed_keys = {(t[0], t[2]) for t in trades}
    mints = list({t[2] for t in trades} | {a[3] for a in approved})
    imgs = token_info(mints)
    alive = bot_alive()
    fresh = now - mtime <= STALE_S
    copies = evs(run, "copy")
    leaders = evs(run, "leader")

    accounts = {}
    for name, acfg in CFG["accounts"].items():
        st_series = [(ts, s[name]["eq"]) for ts, s in statuses if name in s]
        last_st = next((s[name] for ts, s in reversed(statuses) if name in s), None)
        my_tr = [t for t in trades if t[0] == name]
        closes = [{"id": f"x:{name}:{t[2]}:{int(t[5])}", "mint": t[2], "symbol": t[3], "strategy": t[1], "strategy_name": STRAT_NAME.get(t[1], t[1]),
                   "opened": t[4], "closed": t[5], "hold_s": t[5] - t[4], "size_sol": t[6], "sol_out": t[7], "fees_sol": t[8],
                   "pnl_sol": t[9], "pnl_pct": t[10], "reason": t[11], "phase": t[12], "sol_usd": sol_at(reg_pts, t[5], sol_usd), "img": imgs.get(t[2])} for t in my_tr]
        entries, holdings = [], []
        for ts, a, strat, mint, sym, detail, phase in approved:
            if a != name:
                continue
            d = json.loads(detail or "{}")
            size = d.get("size") or (d.get("route") or {}).get("size")
            entries.append({"id": f"e:{name}:{mint}:{int(ts)}", "ts": ts, "mint": mint, "symbol": sym, "strategy": strat,
                            "strategy_name": STRAT_NAME.get(strat, strat), "size_sol": size, "phase": phase, "signal": d.get("signal"), "source": d.get("source"),
                            "rug_score": d.get("rug_score"), "img": imgs.get(mint), "sol_usd": sol_at(reg_pts, ts, sol_usd)})
            if (name, mint) not in closed_keys:
                raw = (d.get("route") or {}).get("tokens_raw")
                h = {"mint": mint, "symbol": sym, "strategy": strat, "phase": phase, "strategy_name": STRAT_NAME.get(strat, strat), "opened": ts,
                     "size_sol": size, "img": imgs.get(mint), "value_sol": None, "upnl_sol": None, "upnl_pct": None, "value_src": None, "tokens_raw": raw}
                if raw and alive:
                    v = sell_quote(mint, raw * (1 - LAT))
                    if v is not None and size:
                        h["value_sol"] = v
                        h["upnl_sol"] = v - size - 2 * fee_of(strat)
                        h["upnl_pct"] = h["upnl_sol"] / size * 100
                        h["value_src"] = "Jupiter sell quote"
                holdings.append(h)
        wins = [t for t in closes if t["pnl_sol"] > 0]
        eq = last_st["eq"] if last_st else G["start_balance_sol"]
        # agents
        dc = {s: (n, ap, mx) for a, s, n, ap, mx in dec_counts if a == name}
        def strat_card(code, title, desc):
            used = code in acfg["strategies"]
            n, ap, mx = dc.get(code, (0, 0, None))
            t = [x for x in closes if x["strategy"] == code]
            ld = last_dec.get((name, code))
            last = None
            if ld:
                last = {"ts": ld[0], "text": (f"entered {ld[1]}" if ld[2] else f"skipped {ld[1]}: " + "; ".join(plain(r) for r in ld[3].split(";") if r))}
            opn = [h for h in holdings if h["strategy"] == code]
            task = ("not used by this account" if not used else
                    f"holding {', '.join(h['symbol'] for h in opn)}" if opn else
                    {"A": f"watching {len(CFG['wallets']['follow'])} followed wallets for buys", "B": "waiting for 2+ wallets to buy the same token",
                     "C": "scanning established tokens for momentum", "LS": "scanning fresh launches", "V1": "scanning + copying (loose rules)"}[code])
            return {"key": code, "title": title, "desc": desc, "used": used, "task": task, "last": last, "signals": n,
                    "approved": int(ap or 0), "trades": len(t), "wins": sum(1 for x in t if x["pnl_sol"] > 0),
                    "win_rate": (sum(1 for x in t if x["pnl_sol"] > 0) / len(t) * 100) if t else None,
                    "pnl_sol": sum(x["pnl_sol"] for x in t), "open": len(opn), "last_ts": mx, "phases": phase_split(t)}
        rc = reason_counts.get(name, {})
        lr = regs[-1] if regs else None
        agents = [
            {"key": "F", "title": "Market check", "desc": "Gate F: no new A/B/C trades when SOL is falling", "used": bool(acfg.get("use_regime_gate")),
             "task": (("SOL trend OK, trading allowed" if lr[6] else f"blocking new trades: {lr[7]}") if lr else "waiting for first check"),
             "last": {"ts": lr[0], "text": f"SOL ${lr[1]:.2f} · 1h {lr[3]:+.2f}% · 6h {lr[4]:+.2f}% · 24h {lr[5]:+.2f}%"} if lr else None,
             "metrics": [["Checks", len(regs)], ["Blocked", sum(1 for r in regs if not r[6])], ["Trades blocked", rc.get("F_regime_cash", 0)]],
             "last_ts": lr[0] if lr else None},
            {"key": "SAFE", "title": "Safety / anti-rug", "desc": "Rug score, LP lock, holder concentration, sell-route check", "used": True,
             "task": "scoring every token found", "last": {"ts": last_scan[0], "text": f"{last_scan[1]}: rug risk {last_scan[2]}" +
                                                          (f", vetoed ({plain('veto:' + last_scan[3].split(';')[0])[13:]})" if last_scan[3] else ", no veto")} if last_scan else None,
             "metrics": [["Tokens scored", scan_n or 0], ["Vetoed", int(scan_veto or 0)],
                         ["Trades blocked", rc.get("veto", 0) + rc.get("rug_score", 0) + rc.get("rr_after_costs", 0)]],
             "last_ts": last_scan[0] if last_scan else None},
            strat_card("A", "Copy one wallet", "Buys when a followed wallet buys ≥0.3 SOL"),
            strat_card("B", "Multi-whale", "Buys when 2+ followed wallets buy within 1h"),
            strat_card("C", "Momentum", "Older tokens with more buys than sells"),
            strat_card("LS", "Launch sniper", "Fresh launches, 0.05 SOL, 5-min max hold"),
        ]
        if "V1" in acfg["strategies"]:
            agents.append(strat_card("V1", "V1 loose sniper", "Your first loose rules"))
        peak = max([e for _, e in st_series] + [G["start_balance_sol"]])
        dd = (peak - eq) / peak * 100 if peak else 0
        agents.append({"key": "RISK", "title": "Risk manager", "desc": "Position sizing, max positions, loss limits", "used": True,
                       "task": (f"HALTED: {last_st['halt']}" if last_st and last_st.get("halt") else
                                f"{len(holdings)}/{acfg['max_concurrent']} positions open"),
                       "last": {"ts": statuses[-1][0], "text": f"equity {eq:.4f} SOL, drawdown {dd:.2f}% from peak"} if statuses else None,
                       "metrics": [["Drawdown", f"{dd:.2f}%"],
                                   ["Limits", f"{acfg.get('daily_loss_pct', '-')}/{acfg.get('weekly_loss_pct', '-')}/{acfg.get('max_drawdown_pct', '-')}%" if "daily_loss_pct" in acfg else "none"],
                                   ["Trades blocked", rc.get("max_concurrent_positions", 0) + rc.get("ls_slot_full", 0) + rc.get("ls_tier_capital_cap", 0) + rc.get("risk_breaker", 0)]],
                       "last_ts": statuses[-1][0] if statuses else None})
        # activity
        act = []
        for e in entries:
            act.append({"ts": e["ts"], "type": "buy", "title": f"Bought {e['symbol']}", "sub": (f"{e['strategy_name']} · {e['size_sol']:.4f} SOL" if e["size_sol"] else e["strategy_name"]) + (f" · {e['phase']}" if e.get("phase") else ""),
                        "mint": e["mint"], "img": e["img"], "sol": -(e["size_sol"] or 0), "sol_usd": e["sol_usd"]})
        for x in closes:
            act.append({"ts": x["closed"], "type": "sell", "title": f"Sold {x['symbol']}", "sub": f"{x['strategy_name']} · {x['reason'].replace('_', ' ')} · held {int(x['hold_s'])}s" + (f" · {x['phase']}" if x.get("phase") else ""),
                        "mint": x["mint"], "img": x["img"], "sol": x["pnl_sol"], "pct": x["pnl_pct"], "sol_usd": x["sol_usd"]})
        for ts, d in leaders:
            mint8 = d.get("mint8") or (d.get("mint") or "")[:8]
            why = [r for r in rej if r[1] == name and r[2] in ("A", "B", "V1") and r[3].startswith(mint8) and abs(r[0] - ts) < 120]
            ent = [e for e in entries if e["mint"].startswith(mint8) and abs(e["ts"] - ts) < 120]
            if d.get("nopair"):
                verdict = "skipped: no DEX pair data"
            elif ent:
                verdict = "copied → entered"
            elif why:
                verdict = "skipped: " + "; ".join(dict.fromkeys(plain(r) for w in why for r in w[5].split(";") if r))
            elif not any(s in acfg["strategies"] for s in ("A", "B", "V1")):
                verdict = "not a copy strategy in this account"
            else:
                verdict = "no decision logged"
            act.append({"ts": ts, "type": "copy", "title": f"{d['wallet']} bought {d.get('symbol') or mint8 + '…'}",
                        "sub": (f"~{d['sol']:.2f} SOL · liq ${d['liq']:,} · " if d.get("liq") is not None else "") + (f"{d['phase']} · " if d.get("phase") else "") + verdict,
                        "mint": d.get("mint"), "skipped": not ent})
        for r in rej[:60]:
            if r[1] != name or r[2] in ("A", "B"):
                continue
            act.append({"ts": r[0], "type": "skip", "title": f"Skipped {r[4]}", "sub": f"{STRAT_NAME.get(r[2], r[2])}: " + "; ".join(dict.fromkeys(plain(x) for x in r[5].split(";") if x)),
                        "mint": r[3]})
        act.sort(key=lambda a: -a["ts"])
        accounts[name] = {
            "equity": eq, "start": G["start_balance_sol"], "open": len(holdings), "closed_n": len(closes), "halt": last_st.get("halt") if last_st else None,
            "series": st_series, "closes": list(reversed(closes)), "holdings": holdings, "entries": list(reversed(entries)),
            "stats": {"trades": len(closes), "win_rate": len(wins) / len(closes) * 100 if closes else None,
                      "best": max(closes, key=lambda x: x["pnl_sol"]) if closes else None,
                      "pnl_sol": sum(x["pnl_sol"] for x in closes)},
            "agents": agents, "activity": act[:150], "copy_signals": len(copies), "phases": phase_split(closes),
        }
    try:
        m4 = m4_view.build(sol_at, reg_pts, sol_usd, token_info, plain, phase_split)
        if m4:
            accounts["M4"] = m4
    except Exception as e:
        STATE["m4_err"] = repr(e)
    return {"runner": runner_status(), "run": run, "now": now, "log_mtime": mtime, "fresh": fresh, "bot_alive": alive, "sol_usd": sol_usd,
            "started": statuses[0][0] if statuses else None, "last_cycle": cycles[-1][1] if cycles else None,
            "last_cycle_ts": cycles[-1][0] if cycles else None, "accounts": accounts, "rules": rules_text(), "mode": MODE, "wallets": wallets()}


def rules_text():
    S, R, LS = CFG["strategy"], CFG["accounts"]["REVISED"], CFG["accounts"]["REVISED"]["ls"]
    rg, cr = CFG["regime"], R["crit"]
    return [
        {"title": "Market check", "lines": [f"No new copy/momentum trades if SOL falls more than {-rg['sol_h1_min_pct']}% in 1h, {-rg['sol_h6_min_pct']}% in 6h or {-rg['sol_h24_min_pct']}% in 24h."]},
        {"title": "Safety gate", "lines": [f"Rug risk score {R['max_rug_score']} or less.", f"At least {cr['min_lp_locked_pct']}% of liquidity locked or burned.",
                                           f"Top 10 holders own at most {cr['max_top10_pct']}%, no single holder above {cr['max_single_pct']}%.",
                                           f"Buy-then-sell cost under {cr['max_round_trip_loss_pct']}%; pools that can be pulled (CLMM-only) are rejected.",
                                           f"Reward-to-risk at least {R['min_rr']}:1 after fees."]},
        {"title": "Sizing & limits", "lines": [f"Up to {R['max_position_pct']}% of balance per trade (~{R['max_position_pct'] / 100 * G['start_balance_sol']:.1f} SOL), min {R['min_position_sol']} SOL, max {R['max_concurrent']} open.",
                                               f"Each stop is sized so a loss costs about {R['risk_per_trade_pct']}% of the balance.",
                                               f"Stops for the day at -{R['daily_loss_pct']}%, the week at -{R['weekly_loss_pct']}%, fully at -{R['max_drawdown_pct']}% drawdown."]},
        {"title": "Copy one wallet (A)", "lines": [f"A followed wallet buys ≥{CFG['wallets']['min_leader_buy_sol']} SOL of a token with ≥${S['A']['min_liquidity_usd']:,} liquidity that hasn't pumped >{S['A']['max_m5_pump_pct']}% in 5 min.",
                                                   f"Stop -{S['A']['stop_loss_pct']}%, take profit +{S['A']['take_profit_pct']}%, trailing {S['A']['trailing_stop_pct']}% after +{S['A']['trailing_activate_pct']}%, max hold {S['A']['max_hold_minutes']} min."]},
        {"title": "Multi-whale (B)", "lines": [f"{S['B']['min_wallets']}+ followed wallets buy the same token within {CFG['wallets']['multi_whale_window_minutes']} min (≥${S['B']['min_liquidity_usd']:,} liquidity).",
                                               f"Stop -{S['B']['stop_loss_pct']}%, take profit +{S['B']['take_profit_pct']}%, max hold {S['B']['max_hold_minutes']} min."]},
        {"title": "Momentum (C)", "lines": [f"Tokens older than {S['C']['min_age_minutes']} min with ≥${S['C']['min_liquidity_usd']:,} liquidity and ≥{S['C']['min_buy_sell_ratio_h1']}x more buys than sells in 1h.",
                                            f"Stop -{S['C']['stop_loss_pct']}%, take profit +{S['C']['take_profit_pct']}%, trailing {S['C']['trailing_stop_pct']}% after +{S['C']['trailing_activate_pct']}%, max hold {S['C']['max_hold_minutes']} min."]},
        {"title": "Launch sniper (LS)", "lines": [f"Tokens under {LS['max_age_minutes']} min old with ≥${LS['min_liquidity_usd']:,} liquidity, ≥{LS['min_buys_5m']} buys in 5 min, buy/sell ≥{LS['min_buy_sell_ratio']}, rug risk ≤{LS['max_rug_score']}.",
                                                  f"{LS['position_sol']} SOL per trade, {LS['max_concurrent']} at a time, tier capped at {LS['tier_capital_pct']}% of start.",
                                                  f"Stop -{LS['stop_loss_pct']}%, take profit +{LS['take_profit_pct']}%, trailing {LS['trailing_stop_pct']}% after +{LS['trailing_activate_pct']}%, max hold {LS['max_hold_minutes']} min."]},
        {"title": "Copied wallets", "lines": [f"{'✓ VERIFIED' if w['verified'] else 'UNVERIFIED'} · {w['label']}: {w['evidence']}" for w in wallets()]},
        *([{"title": "Pump.fun only", "lines": [
            "Only pump.fun tokens are traded: ones still on the bonding curve and ones that graduated to PumpSwap/Raydium, tracked as separate buckets.",
            f"Extra pump.fun checks: dev holds ≤{cr.get('max_dev_hold_pct')}%, bundled launch buys ≤{cr.get('max_bundle_pct')}%, dev with ≥{cr.get('serial_launcher_coins')} past coins is flagged as a serial launcher.",
            f"Bonding-curve tokens: liquidity ≥${S['A'].get('min_liquidity_bonding_usd', 0):,} for copy trades; momentum needs age ≥{S['C'].get('bonding_min_age_minutes')} min, curve progress ≥{S['C'].get('bonding_min_progress_pct')}%, ≥{S['C'].get('bonding_min_buys_5m')} buys in 5 min.",
            f"Sniper on the curve: progress between {LS.get('min_progress_pct')}% and {LS.get('max_progress_pct')}%."]}] if MODE == "pump" else []),
        *m4_view.rules(),
        {"title": "Paper mode", "lines": ["Simulated trades only: real live prices and real Jupiter quotes, no wallet, no keys, no transactions."]},
    ]


STATE = {"data": None, "ver": 0}


def refresher():
    while True:
        try:
            STATE["data"] = enrich(build_state(), CFG, os.path.join(BOT, "data", DBF))
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
