"""App view of the multi-agent paper bots (V1, S5 and S6). READ-ONLY over <BOT_DIR>/data/{v1,s5,s6}_bot_*.
Each account = one shared 1 SOL paper bankroll; agents = its sub-agents (with their on/off switch and P/L) + the risk manager."""
import json, os, re, sqlite3, time, tomllib
import agent_exits_plan

BOT = os.environ.get("BOT_DIR", "/workspace/solana-bot")
EMO_DESC = {
    "V1_SNIPE": "New pump.fun launches (≤15 min) with ≥25 buys in 5 min, after the V1 rug filter",
    "V1_SCANNER": "Scanner score ≥0.6 with more buys than sells, after the V1 rug filter",
    "COPY": "Buys when a watched wallet buys ≥0.3 SOL (V1 rug filter); sells when that wallet sells",
    "C_GRAD": "Graduated tokens ≥30 min old with steady buying, market gate + safety score + reward/risk ≥2",
    "LS": "Fresh bonding-curve launches (10–85% progress, ≥25 buys in 5 min), 5-min max hold",
    "M4_SCAN": "New tokens with real volume after pump.fun anti-rug checks; pulls initials at 2x",
    "REVIVAL": "Quiet coin ≥30 min old, ≥50% below its peak, wakes up with ≥4 SOL of buying",
    "S6_COPY": "Buys when a watched wallet buys ≥0.3 SOL (V1 rug filter, ≤$1M market cap, slingoor not copied); out if that wallet sells before 2x",
    "S6_CONFL": "Buys when 2+ different watched wallets bought the same token within 60 min (V1 rug filter, ≤$1M market cap)",
    "S6_CONFIRM": "After a watched wallet buys, waits 3 min and buys only if the price is up 5–50% (V1 rug filter, ≤$1M market cap)",
    "S6_CGRAD": "Momentum C on graduated tokens (market gate + safety score + reward/risk ≥2), ≤$1M market cap",
}


def cfgs():
    A = tomllib.load(open(os.path.join(BOT, "agents.toml"), "rb"))
    B = {b: tomllib.load(open(os.path.join(BOT, f"{b.lower()}_bot.toml"), "rb")) for b in ("V1", "S5", "S6") if os.path.exists(os.path.join(BOT, f"{b.lower()}_bot.toml"))}
    return A, B


def wallets():
    try:
        W = tomllib.load(open(os.path.join(BOT, "wallets.toml"), "rb")).get("follow") or []
    except Exception:
        return []
    return [{"label": w["label"], "address": w["address"], "verified": w.get("evidence", "").lower().startswith(("verified", "on-chain")),
             "evidence": re.sub(r"^(verified on-chain|unverified)[^:]*:\s*", "", w.get("evidence", ""))} for w in W]


def legacy():
    try:
        return json.load(open(os.path.join(BOT, "data", "legacy_agents.json")))
    except Exception:
        return None


def build(bot, sol_at, reg_pts, sol_usd, imgs_fn, plain, phase_split):
    sf, dbf = os.path.join(BOT, "data", f"{bot.lower()}_bot_state.json"), os.path.join(BOT, "data", f"{bot.lower()}_bot.db")
    if not os.path.exists(sf) or not os.path.exists(dbf):
        return None
    A, B = cfgs()
    AG, BC = A["agent"], B[bot]
    st = json.load(open(sf))
    run, now = st["run"], time.time()
    ok = now - st["ts"] < 45
    sol_usd = st.get("sol_usd") or sol_usd
    c = sqlite3.connect(f"file:{dbf}?mode=ro", uri=True, timeout=5)
    trades = c.execute("SELECT agent, mint, symbol, opened, closed, size_sol, sol_out, fees_sol, pnl_sol, pnl_pct, exit_reason, phase, record FROM trades WHERE run=? ORDER BY closed", (run,)).fetchall()
    approved = c.execute("SELECT ts, agent, mint, symbol, detail, phase FROM decisions WHERE run=? AND approved=1 ORDER BY ts", (run,)).fetchall()
    rej = c.execute("SELECT ts, agent, mint, symbol, reasons, phase, detail FROM decisions WHERE run=? AND approved=0 ORDER BY ts DESC LIMIT 80", (run,)).fetchall()
    copies = c.execute("SELECT ts, mint, symbol, approved, reasons, detail FROM decisions WHERE run=? AND agent='COPY' ORDER BY ts DESC LIMIT 40", (run,)).fetchall()
    per_dec = {a: (n, ap, mx) for a, n, ap, mx in c.execute("SELECT agent, COUNT(*), SUM(approved), MAX(ts) FROM decisions WHERE run=? GROUP BY agent", (run,))}
    lastd = {}
    for a in per_dec:
        lastd[a] = c.execute("SELECT ts, symbol, approved, reasons FROM decisions WHERE run=? AND agent=? ORDER BY ts DESC LIMIT 1", (run, a)).fetchone()
    reason_counts = {}
    for (reasons,) in c.execute("SELECT reasons FROM decisions WHERE run=? AND approved=0 ORDER BY ts DESC LIMIT 3000", (run,)):
        for r in reasons.split(";"):
            k = re.split(r"[(:]", r)[0]
            reason_counts[k] = reason_counts.get(k, 0) + 1
    fills = c.execute("SELECT agent, mint, symbol, opened, ts, kind, frac, sol_out, mark, recovered_sol, size_sol, reason FROM fills WHERE run=? AND final=0 ORDER BY ts", (run,)).fetchall()
    series = [(t, e) for t, e in c.execute("SELECT ts, eq FROM equity WHERE run=? ORDER BY ts", (run,))]
    c.close()
    title = lambda a: (AG.get(a) or {}).get("title", a)
    mints = list({t[1] for t in trades} | {x[2] for x in approved} | {h["mint"] for h in st["holdings"]})
    imgs = imgs_fn(mints)
    closes = []
    for t in trades:
        rec = json.loads(t[12] or "{}")
        closes.append({"id": f"x:{bot}:{t[1]}:{int(t[4])}", "mint": t[1], "symbol": t[2], "strategy": t[0], "strategy_name": title(t[0]), "opened": t[3], "closed": t[4],
                       "hold_s": t[4] - t[3], "size_sol": t[5], "sol_out": t[6], "fees_sol": t[7], "pnl_sol": t[8], "pnl_pct": t[9], "reason": t[10], "phase": t[11],
                       "sol_usd": sol_at(reg_pts, t[4], sol_usd), "img": imgs.get(t[1]), "entry_mark": rec.get("entry_mark"), "exit_mark": rec.get("exit_mark"),
                       "peak_mark": rec.get("peak_mark"), "exit_plan": rec.get("exit_plan"), "sells": rec.get("sells"), "wallets": rec.get("wallets"),
                       "initials_pulled": any(x.get("kind") == "initials" for x in rec.get("sells") or [])})
    entries = []
    for ts, a, mint, sym, detail, phase in approved:
        d = json.loads(detail or "{}")
        entries.append({"id": f"e:{bot}:{mint}:{int(ts)}", "ts": ts, "mint": mint, "symbol": sym, "strategy": a, "strategy_name": title(a), "size_sol": d.get("size"),
                        "phase": phase, "signal": d.get("signal"), "rug_score": d.get("rug_score"), "img": imgs.get(mint), "sol_usd": sol_at(reg_pts, ts, sol_usd)})
    partials = [{"id": f"p:{bot}:{f[1]}:{int(f[4])}", "ts": f[4], "mint": f[1], "symbol": f[2], "opened": f[3], "frac": f[6], "sol_out": f[7], "mark": f[8],
                 "recovered_sol": f[9], "size_sol": f[10], "strategy": f[0], "strategy_name": title(f[0]), "img": imgs.get(f[1]), "kind": f[5],
                 "sol_usd": sol_at(reg_pts, f[4], sol_usd), "reason": f[11]} for f in fills]
    fee = lambda a: A["fees"][(AG.get(a) or {}).get("fees", "snipe")]["sol"]
    holdings = []
    for h in st["holdings"]:
        a = h["agent"]
        v = h.get("value_sol") if ok else None
        pl = h.get("plan") or {}
        x = {"id": f"h:{bot}:{h['mint']}:{int(h['opened'])}", "mint": h["mint"], "symbol": h["symbol"], "strategy": a, "strategy_name": title(a), "phase": h["phase"],
             "opened": h["opened"], "size_sol": h["size_sol"], "img": imgs.get(h["mint"]), "value_sol": v, "upnl_sol": None, "upnl_pct": None,
             "value_src": "Jupiter sell quote" if v is not None else None, "entry_mark": h["entry_mark"], "stop_mark": h.get("stop_mark"),
             "recovered_sol": h.get("recovered_sol") or 0, "left_pct": h["tokens"] / h["tokens0"] * 100 if h.get("tokens0") else None, "exit_plan": pl,
             "trail_pct": h.get("trail_pct"), "wallets": h.get("wallets")}
        eng = (AG.get(a) or {}).get("exit", {}).get("engine")
        if eng == "m4":
            x["stage"] = h.get("stage")
        elif eng == "card" and pl.get("take_profits") and (AG.get(a) or {}).get("exit", {}).get("trail_after_tp_pct"):
            X6 = AG[a]["exit"]
            x["status"] = (f"Stake taken out at {X6['take_profits'][0][0]:g}x ✓ · {x['left_pct']:.0f}% left rides · trailing {X6['trail_after_tp_pct']}% · all out at {X6['take_profits'][-1][0]:g}x"
                           if (h.get("tp_done") or 0) > 0 else
                           f"Stop −{X6['stop_loss_pct']}% · stake out at {X6['take_profits'][0][0]:g}x · max {X6['max_hold_minutes']} min" + (f" · copying {', '.join(h['wallets'])}" if h.get("wallets") else ""))
        elif eng == "card":
            x["status"] = (f"TP1 taken ✓ · {x['left_pct']:.0f}% left · trailing stop {pl.get('trailing_stop_pct')}%" if (h.get("tp_done") or 0) > 0 else
                           f"Stop −{pl.get('stop_loss_pct')}% · sells half at +{pl.get('take_profit_pct')}% · max {pl.get('max_hold_minutes')} min")
        else:
            x["status"] = f"Stop −{pl.get('stop_loss_pct')}% · TP +{pl.get('take_profit_pct')}% · max {pl.get('max_hold_minutes')} min" + (f" · copying {', '.join(h['wallets'])}" if h.get("wallets") else "")
        if v is not None:
            x["upnl_sol"] = v + x["recovered_sol"] - h["size_sol"] - (h.get("fees") or 0) - fee(a)
            x["upnl_pct"] = x["upnl_sol"] / h["size_sol"] * 100
        holdings.append(x)
    wins = [t for t in closes if t["pnl_sol"] > 0]
    eq = st["equity"]
    sw = st.get("switches") or {}
    master = sw.get("_master", True)
    subs, cards = [], []
    for a in BC["agents"]:
        S = AG[a]
        t = [x for x in closes if x["strategy"] == a]
        opn = [h for h in holdings if h["strategy"] == a]
        n, ap, mx = per_dec.get(a, (0, 0, None))
        ld = lastd.get(a)
        last = {"ts": ld[0], "text": (f"entered {ld[1]}" if ld[2] else f"skipped {ld[1]}: " + "; ".join(plain(r) for r in ld[3].split(";")[:3] if r))} if ld else None
        on = bool(sw.get(a, BC["agents"][a]))
        tw = [x for x in t if x["pnl_sol"] > 0]
        info = {"key": a, "title": S["title"], "origin": S["origin"], "on": on, "effective": on and master, "default_on": bool(BC["agents"][a]),
                "trades": len(t), "wins": len(tw), "pnl_sol": sum(x["pnl_sol"] for x in t), "open": len(opn),
                "win_rate": len(tw) / len(t) * 100 if t else None, "legacy": ((legacy() or {}).get("by_key") or {}).get(a)}
        subs.append(info)
        task = (f"holding {', '.join(h['symbol'] for h in opn)}" if opn else
                "switched off: no new entries" if not (on and master) else
                {"COPY": f"watching {st.get('wallets_watched', 0)} wallets for buys", "REVIVAL": "watching recently traded coins for a revival",
                 "M4_SCAN": "scanning new pump.fun tokens", "LS": "scanning fresh launches", "C_GRAD": "scanning graduated tokens for momentum",
                 "S6_COPY": f"watching {st.get('wallets_watched', 0)} wallets for buys", "S6_CONFL": "waiting for 2+ watched wallets to buy the same token",
                 "S6_CONFIRM": "checking watched-wallet buys 3 min later", "S6_CGRAD": "scanning graduated tokens ≤$1M for momentum"}.get(a, "scanning new pump.fun tokens"))
        cards.append({"key": a, "title": S["title"], "desc": EMO_DESC.get(a, S["origin"]), "origin": S["origin"], "used": True, "toggle": True, "on": on,
                      "effective": on and master, "task": task, "last": last, "signals": n or 0, "approved": int(ap or 0), "trades": len(t), "wins": len(tw),
                      "win_rate": info["win_rate"], "pnl_sol": info["pnl_sol"], "open": len(opn), "last_ts": mx, "phases": phase_split(t), "legacy": info["legacy"]})
    peak = max([e for _, e in series] + [st["start"]])
    dd = (peak - eq) / peak * 100 if peak else 0
    R = BC["risk"]
    rdesc = (f"{R['position_pct']}% of cash per trade (max {R['max_position_sol']} SOL) · max {R['max_open']} open"
             + (f" · daily auto-stop −{R['daily_stop_pct']:g}%" if R.get("daily_stop_pct") else "") + (f" · entries ≤${R['max_mcap_usd'] / 1e6:g}M market cap" if R.get("max_mcap_usd") else "")
             if R["sizing"] == "v1" else
             f"{R['risk_per_trade_pct']:g}% risk per trade · max {R['max_open']} open · daily auto-stop −{R['daily_stop_pct']:g}%")
    cards.append({"key": "RISK", "title": "Risk manager", "desc": "Shared bankroll: " + rdesc, "used": True,
                  "task": f"HALTED: {st['halted']}" if st.get("halted") else f"{len(holdings)}/{R['max_open']} positions open · today {st.get('day_pnl_pct', 0):+.2f}%",
                  "last": {"ts": st["ts"], "text": f"equity {eq:.4f} SOL, drawdown {dd:.2f}% from peak"},
                  "metrics": [["Drawdown", f"{dd:.2f}%"], ["Today", f"{st.get('day_pnl_pct', 0):+.2f}%"], ["Cash", f"{st['balance']:.4f}"]], "last_ts": st["ts"]})
    act = []
    for e in entries:
        act.append({"ts": e["ts"], "type": "buy", "title": f"Bought {e['symbol']}", "sub": f"{e['strategy_name']} · {(e['size_sol'] or 0):.4f} SOL · {e['phase']}",
                    "mint": e["mint"], "img": e["img"], "sol": -(e["size_sol"] or 0), "sol_usd": e["sol_usd"]})
    for p in partials:
        act.append({"ts": p["ts"], "type": "partial", "title": f"{'Initials pulled' if p['kind'] == 'initials' else 'Took profit'} · {p['symbol']}",
                    "sub": f"{p['strategy_name']} · sold {p['frac'] * 100:.0f}% for {p['sol_out']:.4f} SOL", "mint": p["mint"], "img": p["img"], "sol": p["sol_out"], "sol_usd": p["sol_usd"]})
    for x in closes:
        act.append({"ts": x["closed"], "type": "sell", "title": f"Sold {x['symbol']}", "sub": f"{x['strategy_name']} · {x['reason'].replace('_', ' ')} · held {int(x['hold_s'])}s · {x['phase']}",
                    "mint": x["mint"], "img": x["img"], "sol": x["pnl_sol"], "pct": x["pnl_pct"], "sol_usd": x["sol_usd"]})
    for ts, mint, sym, apv, reasons, detail in copies:
        d = json.loads(detail or "{}")
        act.append({"ts": ts, "type": "copy", "title": f"{', '.join(d.get('wallets') or ['?'])} bought {sym}",
                    "sub": ("copied → entered" if apv else "skipped: " + "; ".join(dict.fromkeys(plain(z) for z in reasons.split(";") if z))), "mint": mint})
    for r in rej[:40]:
        if r[1] == "COPY":
            continue
        act.append({"ts": r[0], "type": "skip", "title": f"Skipped {r[3]}", "sub": f"{title(r[1])}: " + "; ".join(dict.fromkeys(plain(z) for z in r[4].split(";") if z)), "mint": r[2]})
    act.sort(key=lambda a: -a["ts"])
    plans = {a: agent_exits_plan.plan(AG[a]["exit"]) for a in BC["agents"]}
    return {"title": BC["general"]["title"], "equity": eq, "start": st["start"], "open": len(holdings), "closed_n": len(closes), "halt": st.get("halted"),
            "series": series, "closes": list(reversed(closes)), "holdings": holdings, "entries": list(reversed(entries)), "partials": list(reversed(partials)),
            "stats": {"trades": len(closes), "win_rate": len(wins) / len(closes) * 100 if closes else None, "best": max(closes, key=lambda x: x["pnl_sol"]) if closes else None,
                      "pnl_sol": sum(x["pnl_sol"] for x in closes)},
            "agents": cards, "subagents": subs, "switches": sw, "master": master, "switch_rev": st.get("switch_rev", 0), "activity": act[:150],
            "copy_signals": len(copies), "phases": phase_split(closes), "plans": plans, "alive": ok, "pid": st.get("pid"), "run": run,
            "skip_reasons": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])[:12]), "regime": st.get("regime"), "cycles": st.get("cycles"),
            "started": series[0][0] if series else st["ts"], "risk": rdesc}


def rules():
    A, B = cfgs()
    out = []
    for a, S in A["agent"].items():
        X = S["exit"]
        if X["engine"] == "m4":
            ex = f"Stop −{X['stop_loss_pct']}%; at {X['initials_at_x']:g}x sells enough to get the SOL back, the rest trails {X['moonbag_trail_pct']}% (never below entry), all out at {X['moonbag_cap_x']:g}x; max hold {X['max_hold_minutes']}/{X['moonbag_max_hold_minutes']} min."
        elif X["engine"] == "card" and X.get("trail_after_tp_pct"):
            ex = (f"Stop −{X['stop_loss_pct']}%" + (f"; once up {round((X['trail_arm_x'] - 1) * 100)}% a {X['trail_pct']}% trailing stop protects the gain" if X.get("trail_arm_x") else "")
                  + f"; at {X['take_profits'][0][0]:g}x sells {X['take_profits'][0][1] * 100:.0f}% (the stake back + costs), the rest rides with a {X['trail_after_tp_pct']}% trailing stop"
                  + f" and is all sold at {X['take_profits'][-1][0]:g}x; max hold {X['max_hold_minutes']} min ({X.get('max_hold_after_tp_minutes', X['max_hold_minutes'])} min after 2x).")
        elif X["engine"] == "card":
            ex = f"Stop −{X['stop_loss_pct']}%; sells half at {X['take_profits'][0][0]:g}x, the rest at {X['take_profits'][-1][0]:g}x or on a {X['trail_pct']}% trailing stop; max hold {X['max_hold_minutes']} min."
        else:
            ex = (f"Stop −{X['stop_loss_pct']}%, take profit +{X['take_profit_pct']}%" + (f", trailing {X['trailing_stop_pct']}% after +{X['trailing_activate_pct']}%" if X.get("trailing_stop_pct") else "")
                  + (f", break-even stop after +{X['breakeven_at_pct']}%" if X.get("breakeven_at_pct") else "") + f", max hold {X['max_hold_minutes']} min"
                  + (f", exit if liquidity drops {X['liq_drop_exit_pct']}%" if X.get("liq_drop_exit_pct") else "") + ".")
        hosts = [b for b in B if a in B[b]["agents"]]
        if not hosts:
            continue
        out.append({"title": S["title"], "key": a, "lines": [EMO_DESC.get(a, ""), ("Exits (S6 rule): " if a.startswith("S6_") else "Exits (unchanged from its original bot): ") + ex,
                                                              f"From: {S['origin']}. Runs in: {', '.join(hosts)}."]})
    out.append({"title": "Shared bankroll & limits", "lines": [
        "V1: one 1 SOL paper bankroll for all its sub-agents; 15% of cash per trade (max 0.15 SOL, min 0.05), max 3 open.",
        "S5: one 1 SOL paper bankroll for all its sub-agents; 1% of equity at risk per trade (via each sub-agent's own stop, max 10% per position), max 3 open, no new trades for the rest of the Dubai day once it is down 3%.",
        "S6: one 1 SOL paper bankroll for its 4 sub-agents; 8% of cash per trade (max 0.08 SOL, min 0.03), max 4 open, no new trades for the rest of the Dubai day once it is down 10%; pump.fun tokens only, no entry above $1M market cap.",
        "One position per token per bot; a token is not bought twice (S5 Revival: 60 s cool-down)."]})
    out.append({"title": "On/off switches", "lines": [
        "Each sub-agent and each bot has a switch in the Agents tab. Off = no new entries; open positions keep their exits.",
        "Switches are sent as encrypted, signed commands over the private relay (only your private link can send them), applied within seconds and kept across restarts.",
        "The app has no trade, wallet or deposit buttons."]})
    return out
