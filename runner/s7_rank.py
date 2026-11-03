"""S7 wallet ranking (PAPER ONLY, pure functions, no network). Decides which watched wallets S7 copies and how big.

Data per wallet = a chronological list of trade results {"ts": close time, "ret": pnl / stake}:
  * seed: the replay of that wallet's past buys with S7's own exits (runner/s7_seed.json, built offline from the bots' logs), and
  * own:  S7's own closed copy trades of that wallet (they are appended as they close, so the ranking re-ranks itself).
Rule (simple on purpose):
  1. Score on the wallet's last `window` results (seed + own): n, wins, net (sum of returns, in stakes), profit factor.
  2. DROPPED (never copied again this run) when S7's OWN trades of the wallet show `drop_consec_losses` losses in a row,
     or a negative net over its last `drop_last_n` own trades.
  3. Eligible = not dropped, n >= min_trades, net > 0 and profit factor >= min_pf.
  4. Eligible wallets sorted by net (then PF): the best `top_n` that also have n >= top_min_trades and PF >= top_min_pf are tier 1;
     the other eligible ones fill tier 2 up to `max_copied` wallets in total. Everyone else is tier 0 (not copied).
Tier 1 = the bigger size (S7_TOP agent), tier 2 = the smaller size (S7_NEXT agent)."""

DEFAULT = dict(window=20, min_trades=3, min_pf=1.0, top_n=2, top_min_trades=5, top_min_pf=1.5, max_copied=5,
               drop_consec_losses=3, drop_last_n=5)


def stats(rets):
    n = len(rets)
    gw = sum(r for r in rets if r > 0)
    gl = -sum(r for r in rets if r <= 0)
    return {"n": n, "wins": sum(1 for r in rets if r > 0), "net": sum(rets), "pf": (gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0))}


def drop_reason(own_rets, cfg):
    k, m = cfg["drop_consec_losses"], cfg["drop_last_n"]
    if k and len(own_rets) >= k and all(r <= 0 for r in own_rets[-k:]):
        return f"{k} losses in a row"
    if m and len(own_rets) >= m and sum(own_rets[-m:]) < 0:
        return f"negative over its last {m} trades"
    return None


def rank(labels, seed, own, cfg=None):
    """labels: watched wallet labels. seed/own: {label: [{"ts":..,"ret":..}, ...]}. Returns rows sorted best first:
    {"wallet", "tier" (1/2/0), "n", "wins", "win_rate", "net", "pf", "own_n", "own_net", "dropped", "why"}."""
    c = dict(DEFAULT, **(cfg or {}))
    rows = []
    for w in labels:
        s = sorted(seed.get(w) or [], key=lambda x: x["ts"])
        o = sorted(own.get(w) or [], key=lambda x: x["ts"])
        allr = [x["ret"] for x in sorted(s + o, key=lambda x: x["ts"])][-c["window"]:]
        st = stats(allr)
        orets = [x["ret"] for x in o]
        dr = drop_reason(orets, c)
        rows.append({"wallet": w, "tier": 0, "n": st["n"], "wins": st["wins"], "win_rate": (st["wins"] / st["n"] * 100 if st["n"] else None),
                     "net": st["net"], "pf": st["pf"], "own_n": len(o), "own_net": sum(orets), "dropped": dr, "why": None})
    elig = [r for r in rows if not r["dropped"] and r["n"] >= c["min_trades"] and r["net"] > 0 and r["pf"] >= c["min_pf"]]
    elig.sort(key=lambda r: (-r["net"], -r["pf"], r["wallet"]))
    t1 = 0
    for i, r in enumerate(elig):
        if i >= c["max_copied"]:
            r["why"] = f"not in the best {c['max_copied']}"
            continue
        if t1 < c["top_n"] and r["n"] >= c["top_min_trades"] and r["pf"] >= c["top_min_pf"]:
            r["tier"], t1 = 1, t1 + 1
        else:
            r["tier"] = 2
    for r in rows:
        if r["why"] or r["tier"]:
            continue
        r["why"] = (f"dropped: {r['dropped']}" if r["dropped"] else
                    f"only {r['n']} trade{'s' if r['n'] != 1 else ''} (needs {c['min_trades']})" if r["n"] < c["min_trades"] else
                    "losing (net ≤ 0)" if r["net"] <= 0 else f"profit factor {r['pf']:.2f} < {c['min_pf']:g}")
    order = {1: 0, 2: 1, 0: 2}
    rows.sort(key=lambda r: (order[r["tier"]], -r["net"], r["wallet"]))
    return rows


def tiers(rows):
    return {r["wallet"]: r["tier"] for r in rows}


def size_for_tier(tier, balance, sizing):
    """sizing: {"tier1_pct", "tier1_max_sol", "tier2_pct", "tier2_max_sol", "min_position_sol", "reserve_sol"}. 0.0 = do not copy."""
    if tier not in (1, 2):
        return 0.0
    pct, mx = sizing[f"tier{tier}_pct"], sizing[f"tier{tier}_max_sol"]
    s = min(balance * pct / 100, mx, balance - sizing["reserve_sol"])
    return s if s >= sizing["min_position_sol"] else 0.0
