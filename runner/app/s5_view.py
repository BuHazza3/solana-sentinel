"""Builds the app view of the S5 QUANT paper account (READ-ONLY over /workspace/solana-bot/data/s5_*).
Same shape as the other accounts in server.py; partial sells = the strategy card's take-profit steps."""
import json, os, re, sqlite3, time, tomllib

BOT = os.environ.get("BOT_DIR", "/workspace/solana-bot")
STATE_F = os.path.join(BOT, "data", "s5_state.json")
DB_F = os.path.join(BOT, "data", "s5_quant.db")
BT_F = os.path.join(BOT, "s5_backtest_summary.json")
NAME = "S5 Quant"


def cfg():
    try:
        return tomllib.load(open(os.path.join(BOT, "s5_quant.toml"), "rb"))
    except Exception:
        return None


def fee(C):
    g = C["general"]
    return g["fee_base_sol"] + g["fee_priority_sol"] + g["fee_jito_sol"]


def plan(C):
    if not C:
        return None
    X = C["exit"]
    return {"stop_loss_pct": X["stop_loss_pct"], "take_profit_pct": round((X["take_profits"][0][0] - 1) * 100), "take_profits": X["take_profits"],
            "trailing_stop_pct": X["trail_pct"], "trailing_activate_pct": round((X["take_profits"][0][0] - 1) * 100), "max_hold_minutes": X["max_hold_minutes"]}


def backtest():
    try:
        return json.load(open(BT_F))
    except Exception:
        return None


def build(sol_at, reg_pts, sol_usd, imgs_fn, plain, phase_split):
    if not os.path.exists(STATE_F) or not os.path.exists(DB_F):
        return None
    C = cfg()
    st = json.load(open(STATE_F))
    run, now, FEE = st["run"], time.time(), fee(C)
    ok = now - st["ts"] < 45
    c = sqlite3.connect(f"file:{DB_F}?mode=ro", uri=True, timeout=5)
    trades = c.execute("SELECT mint, symbol, opened, closed, size_sol, sol_out, fees_sol, pnl_sol, pnl_pct, exit_reason, phase, record FROM trades WHERE run=? ORDER BY closed", (run,)).fetchall()
    approved = c.execute("SELECT ts, mint, symbol, detail, phase FROM decisions WHERE run=? AND approved=1 ORDER BY ts", (run,)).fetchall()
    rej = c.execute("SELECT ts, mint, symbol, reasons, phase FROM decisions WHERE run=? AND approved=0 ORDER BY ts DESC LIMIT 60", (run,)).fetchall()
    n_dec, n_app, last_dec_ts = c.execute("SELECT COUNT(*), SUM(approved), MAX(ts) FROM decisions WHERE run=?", (run,)).fetchone()
    reason_counts = {}
    for (reasons,) in c.execute("SELECT reasons FROM decisions WHERE run=? AND approved=0 ORDER BY ts DESC LIMIT 3000", (run,)):
        for r in reasons.split(";"):
            k = re.split(r"[(:]", r)[0]
            reason_counts[k] = reason_counts.get(k, 0) + 1
    fills = c.execute("SELECT mint, symbol, opened, ts, kind, frac, sol_out, mark, recovered_sol, size_sol, reason FROM fills WHERE run=? ORDER BY ts", (run,)).fetchall()
    series = [(t, e) for t, e in c.execute("SELECT ts, eq FROM equity WHERE run=? ORDER BY ts", (run,))]
    c.close()
    mints = list({t[0] for t in trades} | {a[1] for a in approved} | {h["mint"] for h in st["holdings"]})
    imgs = imgs_fn(mints)
    P = plan(C)
    X = C["exit"]
    closes = []
    for t in trades:
        rec = json.loads(t[11] or "{}")
        closes.append({"id": f"x:S5:{t[0]}:{int(t[3])}", "mint": t[0], "symbol": t[1], "strategy": "S5", "strategy_name": NAME, "opened": t[2], "closed": t[3],
                       "hold_s": t[3] - t[2], "size_sol": t[4], "sol_out": t[5], "fees_sol": t[6], "pnl_sol": t[7], "pnl_pct": t[8], "reason": t[9], "phase": t[10],
                       "sol_usd": sol_at(reg_pts, t[3], sol_usd), "img": imgs.get(t[0]), "entry_mark": rec.get("entry_mark"), "exit_mark": rec.get("exit_mark"),
                       "peak_mark": rec.get("peak_mark"), "exit_plan": rec.get("exit_plan") or P, "sells": rec.get("sells")})
    entries = []
    for ts, mint, sym, detail, phase in approved:
        d = json.loads(detail or "{}")
        entries.append({"id": f"e:S5:{mint}:{int(ts)}", "ts": ts, "mint": mint, "symbol": sym, "strategy": "S5", "strategy_name": NAME, "size_sol": d.get("size"),
                        "phase": phase, "signal": "revival", "img": imgs.get(mint), "sol_usd": sol_at(reg_pts, ts, sol_usd)})
    partials = [{"id": f"p:S5:{f[0]}:{int(f[3])}", "ts": f[3], "mint": f[0], "symbol": f[1], "opened": f[2], "frac": f[5], "sol_out": f[6], "mark": f[7],
                 "recovered_sol": f[8], "size_sol": f[9], "strategy": "S5", "strategy_name": NAME, "img": imgs.get(f[0]), "sol_usd": sol_at(reg_pts, f[3], sol_usd),
                 "reason": f[10]}
                for f in fills if f[4] == "partial"]
    holdings = []
    for h in st["holdings"]:
        v = h.get("value_sol") if ok else None
        x = {"id": f"h:S5:{h['mint']}:{int(h['opened'])}", "mint": h["mint"], "symbol": h["symbol"], "strategy": "S5", "phase": h["phase"], "strategy_name": NAME,
             "opened": h["opened"], "size_sol": h["size_sol"], "img": imgs.get(h["mint"]), "value_sol": v, "upnl_sol": None, "upnl_pct": None,
             "value_src": "Jupiter sell quote" if v is not None else None, "entry_mark": h["entry_mark"], "stop_mark": h.get("stop_mark"), "stage": h["stage"],
             "recovered_sol": h["recovered_sol"], "left_pct": h["tokens"] / h["tokens0"] * 100 if h.get("tokens0") else None, "exit_plan": P}
        if v is not None:
            x["upnl_sol"] = v + h["recovered_sol"] - h["size_sol"] - h["fees"] - FEE
            x["upnl_pct"] = x["upnl_sol"] / h["size_sol"] * 100
        tp1, tp2 = X["take_profits"][0], X["take_profits"][-1]
        x["status"] = (f"TP1 taken ✓ ({h['recovered_sol']:.4f} SOL back) · {x['left_pct']:.0f}% left · trailing stop {X['trail_pct']}% · sells the rest at {tp2[0]:g}x"
                       if h["stage"] == "tp1_taken" else
                       f"Stop −{X['stop_loss_pct']}% · sells {tp1[1] * 100:.0f}% at {tp1[0]:g}x · rest at {tp2[0]:g}x or {X['trail_pct']}% trail · max {X['max_hold_minutes']} min")
        holdings.append(x)
    wins = [t for t in closes if t["pnl_sol"] > 0]
    eq = st["equity"]
    last_rej = rej[0] if rej else None
    last_txt = None
    if approved and (not last_rej or approved[-1][0] >= last_rej[0]):
        last_txt = {"ts": approved[-1][0], "text": f"entered {approved[-1][2]}"}
    elif last_rej:
        last_txt = {"ts": last_rej[0], "text": f"skipped {last_rej[2]}: " + "; ".join(plain(r) for r in last_rej[3].split(";")[:3] if r)}
    cnt = st.get("counters") or {}
    bt = backtest()
    card = {"key": "S5", "title": "S5 Quant · Revival", "desc": "Quiet pump.fun coin (≥30 min old, ≥50% below its peak) wakes up with ≥4 SOL of buying", "used": True,
            "task": (f"holding {', '.join(h['symbol'] for h in holdings)}" if holdings else "watching recently traded pump.fun coins for a revival"),
            "last": last_txt, "signals": n_dec or 0, "approved": int(n_app or 0), "trades": len(closes), "wins": len(wins),
            "win_rate": len(wins) / len(closes) * 100 if closes else None, "pnl_sol": sum(t["pnl_sol"] for t in closes), "open": len(holdings),
            "last_ts": last_dec_ts, "phases": phase_split(closes)}
    desk = {"key": "DESK", "title": "Quant desk backtest", "desc": "10 public pump.fun strategies tested on real candles with fees + slippage", "used": True,
            "task": (bt or {}).get("verdict", "no backtest summary"), "last": None, "last_ts": None,
            "metrics": [["Strategies tested", (bt or {}).get("tested", "n/a")], ["Passed the bar", (bt or {}).get("passed", "n/a")],
                        ["S5 backtest PF", (bt or {}).get("s5_pf", "n/a")]]}
    peak = max([e for _, e in series] + [st["start"]])
    dd = (peak - eq) / peak * 100 if peak else 0
    R = C["risk"]
    risk = {"key": "RISK", "title": "Risk manager", "desc": f"{R['risk_per_trade_pct']:g}% risk per trade · max {R['max_open']} open · daily auto-stop at −{R['daily_stop_pct']:g}%", "used": True,
            "task": f"HALTED: {st['halted']}" if st.get("halted") else f"{len(holdings)}/{R['max_open']} positions open · today {st.get('day_pnl_pct', 0):+.2f}%",
            "last": {"ts": st["ts"], "text": f"equity {eq:.4f} SOL, drawdown {dd:.2f}% from peak"},
            "metrics": [["Drawdown", f"{dd:.2f}%"], ["Today", f"{st.get('day_pnl_pct', 0):+.2f}%"], ["Cash", f"{st['balance']:.4f}"]], "last_ts": st["ts"]}
    act = []
    for e in entries:
        act.append({"ts": e["ts"], "type": "buy", "title": f"Bought {e['symbol']}", "sub": f"{NAME} · {(e['size_sol'] or 0):.4f} SOL · {e['phase']} · revival",
                    "mint": e["mint"], "img": e["img"], "sol": -(e["size_sol"] or 0), "sol_usd": e["sol_usd"]})
    for p in partials:
        act.append({"ts": p["ts"], "type": "partial", "title": f"Took profit · {p['symbol']}", "sub": f"{NAME} · {str(p['reason']).replace('_', ' ')} · sold {p['frac'] * 100:.0f}% for {p['sol_out']:.4f} SOL",
                    "mint": p["mint"], "img": p["img"], "sol": p["sol_out"], "sol_usd": p["sol_usd"]})
    for x in closes:
        act.append({"ts": x["closed"], "type": "sell", "title": f"Sold {x['symbol']}", "sub": f"{NAME} · {x['reason'].replace('_', ' ')} · held {int(x['hold_s'])}s · {x['phase']}",
                    "mint": x["mint"], "img": x["img"], "sol": x["pnl_sol"], "pct": x["pnl_pct"], "sol_usd": x["sol_usd"]})
    for r in rej[:40]:
        act.append({"ts": r[0], "type": "skip", "title": f"Skipped {r[2]}", "sub": f"{NAME}: " + "; ".join(dict.fromkeys(plain(z) for z in r[3].split(";") if z)), "mint": r[1]})
    act.sort(key=lambda a: -a["ts"])
    return {"equity": eq, "start": st["start"], "open": len(holdings), "closed_n": len(closes), "halt": st.get("halted"), "series": series,
            "closes": list(reversed(closes)), "holdings": holdings, "entries": list(reversed(entries)), "partials": list(reversed(partials)),
            "stats": {"trades": len(closes), "win_rate": len(wins) / len(closes) * 100 if closes else None, "best": max(closes, key=lambda x: x["pnl_sol"]) if closes else None,
                      "pnl_sol": sum(x["pnl_sol"] for x in closes)},
            "agents": [card, desk, risk], "activity": act[:150], "copy_signals": 0, "phases": phase_split(closes),
            "plans": {"S5": P}, "alive": ok, "pid": st.get("pid"), "skip_reasons": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])[:12]),
            "backtest": bt}


def rules():
    C = cfg()
    if not C:
        return []
    E, X, R = C["entry"], C["exit"], C["risk"]
    bt = backtest() or {}
    tp1, tp2 = X["take_profits"][0], X["take_profits"][-1]
    return [{"title": "S5 Quant · Revival", "lines": [
        "Separate 1 SOL paper account. Built by the quant-desk method: 10 public pump.fun strategies (X, Reddit, arXiv, GitHub, docs) were backtested on real 1-minute candles with fees and slippage. None passed the bar; this is the least-bad one, running as a forward paper test.",
        f"Buys a pump.fun coin at least {E['min_age_minutes']} min old that was quiet (≤{E['quiet_max_volume_sol']:g} SOL traded in the last {E['quiet_window_minutes']} min) and then wakes up: ≥{E['wake_min_sol']:g} SOL of volume and ≥{E['wake_min_sol']:g} SOL of net buying in one minute, while at or below {E['max_price_vs_peak'] * 100:.0f}% of its peak.",
        f"Exits from the strategy card: stop −{X['stop_loss_pct']}%; sells {tp1[1] * 100:.0f}% at {tp1[0]:g}x, the rest at {tp2[0]:g}x or on a {X['trail_pct']}% trailing stop after the first take-profit; everything out after {X['max_hold_minutes']} min.",
        f"Risk: {R['risk_per_trade_pct']:g}% of equity at risk per trade (size ≈ {R['risk_per_trade_pct'] / X['stop_loss_pct'] * 100:.1f}% of equity), max {R['max_open']} open, no new trades for the rest of the Dubai day once it is down {R['daily_stop_pct']:g}%.",
        "Source: Outrider memecoin lanes (outrider.mikodes.com/docs/lanes). No rules borrowed from the other four strategies."]},
        {"title": "S5 backtest (estimate)", "lines": bt.get("lines") or ["No backtest summary file."]}]
