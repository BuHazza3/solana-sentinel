"""Builds the app view of the M4 MOMENTUM paper account (READ-ONLY over /workspace/solana-bot/data/m4_*).
Same shape as the other accounts in server.py, plus partial sells ("initials pulled")."""
import json, os, re, sqlite3, time, tomllib

BOT = os.environ.get("BOT_DIR", "/workspace/solana-bot")
STATE_F = os.path.join(BOT, "data", "m4_state.json")
DB_F = os.path.join(BOT, "data", "m4_momentum.db")
NAME = "M4 Momentum"


def cfg():
    try:
        return tomllib.load(open(os.path.join(BOT, "m4_momentum.toml"), "rb"))
    except Exception:
        return None


def fee(C):
    g = C["general"]
    return g["fee_base_sol"] + g["fee_priority_sol"] + g["fee_jito_sol"]


def plan(C):
    if not C:
        return None
    E = C["exit"]
    return {"stop_loss_pct": E["stop_loss_pct"], "take_profit_pct": round((E["initials_at_x"] - 1) * 100), "initials_at_x": E["initials_at_x"],
            "moonbag_trail_pct": E["moonbag_trail_pct"], "moonbag_cap_x": E["moonbag_cap_x"], "breakeven_after_initials": E["breakeven_after_initials"],
            "max_hold_minutes": E["max_hold_minutes"], "moonbag_max_hold_minutes": E["moonbag_max_hold_minutes"], "liq_drop_exit_pct": E["liq_drop_exit_pct"]}


def alive():
    try:
        return time.time() - json.load(open(STATE_F))["ts"] < 45
    except Exception:
        return False


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
    closes = []
    for t in trades:
        rec = json.loads(t[11] or "{}")
        closes.append({"id": f"x:M4:{t[0]}:{int(t[3])}", "mint": t[0], "symbol": t[1], "strategy": "M4", "strategy_name": NAME, "opened": t[2], "closed": t[3],
                       "hold_s": t[3] - t[2], "size_sol": t[4], "sol_out": t[5], "fees_sol": t[6], "pnl_sol": t[7], "pnl_pct": t[8], "reason": t[9], "phase": t[10],
                       "sol_usd": sol_at(reg_pts, t[3], sol_usd), "img": imgs.get(t[0]), "entry_mark": rec.get("entry_mark"), "exit_mark": rec.get("exit_mark"),
                       "peak_mark": rec.get("peak_mark"), "exit_plan": rec.get("exit_plan") or P, "initials_pulled": bool(rec.get("initials_pulled")),
                       "sells": rec.get("sells"), "leaders": rec.get("leaders"), "tier": rec.get("tier"), "rug_score": rec.get("rug_score")})
    entries = []
    for ts, mint, sym, detail, phase in approved:
        d = json.loads(detail or "{}")
        entries.append({"id": f"e:M4:{mint}:{int(ts)}", "ts": ts, "mint": mint, "symbol": sym, "strategy": "M4", "strategy_name": NAME, "size_sol": d.get("size"),
                        "phase": phase, "signal": d.get("tier"), "source": d.get("source"), "rug_score": d.get("rug_score"), "img": imgs.get(mint),
                        "sol_usd": sol_at(reg_pts, ts, sol_usd), "leaders": list((d.get("leaders") or {}).keys())})
    partials = [{"id": f"p:M4:{f[0]}:{int(f[3])}", "ts": f[3], "mint": f[0], "symbol": f[1], "opened": f[2], "frac": f[5], "sol_out": f[6], "mark": f[7],
                 "recovered_sol": f[8], "size_sol": f[9], "strategy": "M4", "strategy_name": NAME, "img": imgs.get(f[0]), "sol_usd": sol_at(reg_pts, f[3], sol_usd)}
                for f in fills if f[4] == "initials"]
    holdings = []
    for h in st["holdings"]:
        v = h.get("value_sol") if ok else None
        x = {"id": f"h:M4:{h['mint']}:{int(h['opened'])}", "mint": h["mint"], "symbol": h["symbol"], "strategy": "M4", "phase": h["phase"], "strategy_name": NAME,
             "opened": h["opened"], "size_sol": h["size_sol"], "img": imgs.get(h["mint"]), "value_sol": v, "upnl_sol": None, "upnl_pct": None,
             "value_src": "Jupiter sell quote" if v is not None else None, "entry_mark": h["entry_mark"], "stop_mark": h["stop_mark"], "stage": h["stage"],
             "recovered_sol": h["recovered_sol"], "left_pct": h["tokens"] / h["tokens0"] * 100 if h.get("tokens0") else None, "leaders": h.get("leaders"),
             "tier": h.get("tier"), "exit_plan": P, "initials_ts": h.get("initials_ts")}
        if v is not None:
            x["upnl_sol"] = v + h["recovered_sol"] - h["size_sol"] - h["fees"] - FEE
            x["upnl_pct"] = x["upnl_sol"] / h["size_sol"] * 100
        E = C["exit"]
        x["moon_peak"], x["trail_pct"] = h.get("moon_peak"), h.get("trail_pct")
        x["status"] = (f"Initials pulled ✓ ({h['recovered_sol']:.4f} SOL back) · moonbag {x['left_pct']:.0f}% · trail {h.get('trail_pct') or E['moonbag_trail_pct']}% (never below entry) · cap {E['moonbag_cap_x']:g}x"
                       if h["stage"] == "initials_pulled" else
                       f"Stop −{E['stop_loss_pct']}% · pulls initials at {E['initials_at_x']:g}x · then moonbag rides")
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
    card = {"key": "M4", "title": "M4 Momentum", "desc": "New tokens with real volume · pulls initials at 2x · moonbag rides with a trailing stop", "used": True,
            "task": (f"holding {', '.join(h['symbol'] for h in holdings)}" if holdings else "scanning new pump.fun tokens (≤30 min) for volume + buyers"),
            "last": last_txt, "signals": n_dec or 0, "approved": int(n_app or 0), "trades": len(closes), "wins": len(wins),
            "win_rate": len(wins) / len(closes) * 100 if closes else None, "pnl_sol": sum(t["pnl_sol"] for t in closes), "open": len(holdings),
            "last_ts": last_dec_ts, "phases": phase_split(closes)}
    safety = {"key": "SAFE", "title": "M4 anti-rug", "desc": "Authorities, dev wallet, holders, bundles, creator history, RugCheck, exit route", "used": True,
              "task": "checking every token that passes the volume screen",
              "last": last_txt if last_rej and last_txt and last_txt["text"].startswith("skipped") else None,
              "metrics": [["Checked", n_dec or 0], ["Skipped", (n_dec or 0) - int(n_app or 0)],
                          ["Rug checks failed", sum(v for k, v in reason_counts.items() if k in ANTIRUG_KEYS)]], "last_ts": last_dec_ts}
    copyc = {"key": "COPY", "title": "Copy-wallet boost", "desc": "Tracked profitable wallets: relaxed volume floor when they buy, exit when they sell", "used": True,
             "task": f"watching {_wallet_n()} tracked wallets",
             "last": None, "metrics": [["Wallet buys", cnt.get("leader_buys_seen", 0)], ["Wallet sells", cnt.get("leader_sells_seen", 0)],
                                       ["Copied entries", sum(1 for e in entries if e.get("signal") in ("leader", "multi_leader"))]], "last_ts": None}
    peak = max([e for _, e in series] + [st["start"]])
    dd = (peak - eq) / peak * 100 if peak else 0
    risk = {"key": "RISK", "title": "Risk manager", "desc": "Fixed 0.05 SOL per trade, max 3 open, halt at −30%", "used": True,
            "task": f"HALTED: {st['halted']}" if st.get("halted") else f"{len(holdings)}/{C['risk']['max_concurrent']} positions open",
            "last": {"ts": st["ts"], "text": f"equity {eq:.4f} SOL, drawdown {dd:.2f}% from peak"},
            "metrics": [["Drawdown", f"{dd:.2f}%"], ["Cash", f"{st['balance']:.4f}"], ["Trades blocked", reason_counts.get("max_concurrent_positions", 0)]], "last_ts": st["ts"]}
    act = []
    for e in entries:
        act.append({"ts": e["ts"], "type": "buy", "title": f"Bought {e['symbol']}", "sub": f"{NAME} · {e['size_sol']:.4f} SOL · {e['phase']}" +
                    (f" · copied {', '.join(e['leaders'])}" if e.get("leaders") else ""), "mint": e["mint"], "img": e["img"], "sol": -(e["size_sol"] or 0), "sol_usd": e["sol_usd"]})
    for p in partials:
        act.append({"ts": p["ts"], "type": "partial", "title": f"Pulled initials · {p['symbol']}", "sub": f"{NAME} · sold {p['frac'] * 100:.0f}% at 2x for {p['sol_out']:.4f} SOL (in {p['size_sol']:.4f}) · moonbag rides",
                    "mint": p["mint"], "img": p["img"], "sol": p["sol_out"], "sol_usd": p["sol_usd"]})
    for x in closes:
        act.append({"ts": x["closed"], "type": "sell", "title": f"Sold {x['symbol']}", "sub": f"{NAME} · {x['reason'].replace('_', ' ')} · held {int(x['hold_s'])}s" +
                    (" · initials were pulled" if x["initials_pulled"] else "") + f" · {x['phase']}", "mint": x["mint"], "img": x["img"], "sol": x["pnl_sol"],
                    "pct": x["pnl_pct"], "sol_usd": x["sol_usd"]})
    for r in rej[:40]:
        act.append({"ts": r[0], "type": "skip", "title": f"Skipped {r[2]}", "sub": f"{NAME}: " + "; ".join(dict.fromkeys(plain(z) for z in r[3].split(";") if z)), "mint": r[1]})
    act.sort(key=lambda a: -a["ts"])
    return {"equity": eq, "start": st["start"], "open": len(holdings), "closed_n": len(closes), "halt": st.get("halted"), "series": series,
            "closes": list(reversed(closes)), "holdings": holdings, "entries": list(reversed(entries)), "partials": list(reversed(partials)),
            "stats": {"trades": len(closes), "win_rate": len(wins) / len(closes) * 100 if closes else None, "best": max(closes, key=lambda x: x["pnl_sol"]) if closes else None,
                      "pnl_sol": sum(x["pnl_sol"] for x in closes)},
            "agents": [card, copyc, safety, risk], "activity": act[:150], "copy_signals": cnt.get("leader_buys_seen", 0), "phases": phase_split(closes),
            "plans": {"M4": P}, "alive": ok, "pid": st.get("pid"), "skip_reasons": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])[:12])}


ANTIRUG_KEYS = {"mint_authority_not_revoked", "freeze_authority_not_revoked", "rugcheck_marked_rugged", "rugcheck_score", "rugcheck_danger", "top10_holders",
                "single_holder", "insider_network", "lp_not_locked", "lp_pullable_clmm_only", "serial_launcher", "dev_holds_too_much", "dev_dumping",
                "bundled_launch", "sniper_cluster", "no_sell_route", "exit_cost_too_high", "veto", "rug_score", "no_rugcheck_report", "pumpfun_banned",
                "few_unique_traders"}


def _wallet_n():
    try:
        return len(tomllib.load(open(os.path.join(BOT, "pump_sentinel.toml"), "rb"))["wallets"]["follow"])
    except Exception:
        return 0


def rules():
    C = cfg()
    if not C:
        return []
    E, CP, AR, X, R = C["entry"], C["copy"], C["antirug"], C["exit"], C["risk"]
    return [{"title": "M4 Momentum", "lines": [
        f"Separate 1 SOL paper account. New pump.fun tokens up to {E['max_age_minutes']} min old (bonding curve or just graduated), market cap ${E['min_mcap_usd']:,}–${E['max_mcap_usd'] / 1e6:g}M.",
        f"Needs in the last 5 min: ≥${E['min_vol_5m_usd']:,} volume, ≥{E['min_trades_5m']} trades, buys ≥{E['min_buy_sell_ratio_5m']}× sells, price up ≥{E['min_price_change_5m_pct']}% but not more than {E['max_price_change_5m_pct']}% (no chasing), ≥{E['min_unique_traders']} different traders.",
        f"Skips tokens below {E['min_mcap_vs_ath_pct']}% of their all-time-high market cap. Curve progress {E['bonding_min_progress_pct']}–{E['bonding_max_progress_pct']}%; graduated pools need ≥${E['graduated_min_liquidity_usd']:,} liquidity.",
        f"{R['position_sol']} SOL per trade, max {R['max_concurrent']} open, no new trades if the account drops below {R['halt_below_equity_sol']} SOL.",
        f"Before take profit: stop −{X['stop_loss_pct']}% on the whole position, max hold {X['max_hold_minutes']} min, and it sells if a copied wallet sells.",
        f"At {X['initials_at_x']:g}x it sells just enough to get your SOL and all fees back (initials pulled, about half the position).",
        f"The rest is a free moonbag: trailing stop {X['moonbag_trail_pct']}% below its highest price since then (tightens to {X['moonbag_trail_after_leader_sell_pct']}% if a copied wallet sells), never below the entry price; everything is sold at {X['moonbag_cap_x']:g}x or after {X['moonbag_max_hold_minutes']} min.",
        f"Also exits if trading dies (<{X['dead_max_trades_5m']} trades in 5 min after {X['dead_after_minutes']} min held), price data is lost for {X['price_lost_seconds']}s, or a graduated pool loses {X['liq_drop_exit_pct']}% of its liquidity."]},
        {"title": "M4 copy-wallet boost", "lines": [
        f"Uses the tracked wallets. One wallet buying ≥{CP['min_leader_buy_sol']} SOL within {CP['window_minutes']} min relaxes the activity floor to ${CP['leader_min_vol_5m_usd']:,} volume, {CP['leader_min_trades_5m']} trades, buys ≥{CP['leader_min_buy_sell_ratio_5m']}× sells.",
        f"Two or more wallets: buys ≥{CP['multi_min_buy_sell_ratio_5m']}× sells and tokens up to {E['max_age_minutes_multi_leader']} min old. All anti-rug checks still apply. A tracked wallet that sold recently blocks the entry."]},
        {"title": "M4 anti-rug checks", "lines": [
        "Mint and freeze authority must be revoked; no transfer fee or risky Token-2022 extensions; not banned or flagged by pump.fun; RugCheck not marked rugged.",
        f"Dev holds ≤{AR['max_dev_hold_pct']}% of supply and hasn't sold ≥{AR['max_dev_sold_pct_of_buys']}% of his buys; dev with ≥{AR['serial_launcher_coins']} coins and none graduated is rejected.",
        f"Top 10 holders ≤{AR['max_top10_pct']}% and no single holder >{AR['max_single_holder_pct']}% (bonding curve and pools excluded); insiders ≤{AR['max_insiders']}.",
        f"Launch bundle ≤{AR['max_bundle_pct']}% of supply and ≤{AR['max_launch_block_buyers']} wallets in the launch block; RugCheck score ≤{AR['max_rugcheck_score']} and no RugCheck danger flag (e.g. creator history of rugs).",
        f"Graduated pools: LP ≥{AR['min_lp_locked_pct']}% locked/burned. A real Jupiter buy and sell quote must exist with ≤{E['max_round_trip_loss_pct']}% round-trip loss; pump.fun rug score ≤{E['max_pump_rug_score']}."]}]
