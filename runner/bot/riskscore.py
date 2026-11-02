"""Rug Risk Score 0-100 (SPEC Phase 5) from free data: RugCheck report + DexScreener pair + Jupiter route.
Higher = riskier. Missing data adds an explicit uncertainty penalty. Critical failures are returned as vetoes
(they block a trade regardless of the score). A low score never means "rug-proof"."""
SYSTEM = "11111111111111111111111111111111"
BAD_EXT = {"permanentDelegate", "transferHook", "nonTransferable", "defaultAccountState", "confidentialTransfer"}


def lp_status(report):
    markets = report.get("markets") or []
    lp = [m for m in markets if (m.get("lp") or {}).get("lpMint") not in (None, SYSTEM)]
    if lp:
        tot = sum((m["lp"].get("quoteUSD") or 0) + (m["lp"].get("baseUSD") or 0) for m in lp) or 1
        return "amm", sum(((m["lp"].get("quoteUSD") or 0) + (m["lp"].get("baseUSD") or 0)) * (m["lp"].get("lpLockedPct") or 0) for m in lp) / tot
    if any(m.get("marketType") == "pump_fun" for m in markets):
        return "bonding_curve", None
    if markets:
        return "clmm_only", None
    return "unknown", None


def holders(report):
    exclude = set((report.get("knownAccounts") or {}).keys())
    for m in report.get("markets") or []:
        exclude |= {m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB")}
        for k in ("liquidityAAccount", "liquidityBAccount"):
            if m.get(k):
                exclude.add(m[k].get("owner"))
    hs = [h for h in report.get("topHolders") or [] if h.get("owner") not in exclude and h.get("address") not in exclude]
    if not hs:
        return None, None
    return sum(h.get("pct") or 0 for h in hs[:10]), max(h.get("pct") or 0 for h in hs)


def score(pair, report, route, crit, phase=None):
    """crit = thresholds for critical vetoes. route = dict from route_check or None."""
    pts, vetoes, comp, unc = 0.0, [], {}, []

    def add(name, p):
        nonlocal pts
        if p:
            comp[name] = round(p, 1)
            pts += p

    liq = ((pair or {}).get("liquidity") or {}).get("usd") or 0
    # liquidity depth
    if phase == "bonding":   # curve liquidity cannot be pulled; exit cost is measured directly by the route check
        add("liquidity_curve", 10 if liq < 8000 else 5 if liq < 15000 else 0)
    else:
        add("liquidity", 25 if liq < 20000 else 12 if liq < 50000 else 5 if liq < 100000 else 0)
    if not report:
        add("uncertainty:no_security_report", 45); unc.append("no_rugcheck_report")
    else:
        if report.get("rugged"):
            vetoes.append("rugcheck_marked_rugged")
        if report.get("mintAuthority"):
            vetoes.append("mint_authority_active")
        if report.get("freezeAuthority"):
            vetoes.append("freeze_authority_active")
        # Token-2022 extensions / transfer fee
        tf = (report.get("transferFee") or {}).get("pct") or 0
        if tf > 0:
            vetoes.append(f"transfer_fee_{tf}%")
        ext = report.get("token_extensions")
        names = set()
        if isinstance(ext, dict):
            names = {k for k, v in ext.items() if v}
            if "defaultAccountState" in names and "frozen" not in str(ext.get("defaultAccountState")).lower():
                names.discard("defaultAccountState")
        elif isinstance(ext, list):
            names = {e.get("extension") if isinstance(e, dict) else str(e) for e in ext}
        bad = names & BAD_EXT
        if bad:
            vetoes.append("token2022_risky_ext:" + ",".join(sorted(bad)))
        # LP
        kind, locked = lp_status(report)
        comp["lp_kind"] = kind
        if kind == "amm":
            add("lp_unlocked", (100 - locked) / 100 * 30)
            if locked < crit["min_lp_locked_pct"]:
                vetoes.append(f"lp_not_locked({locked:.0f}%)")
        elif kind == "bonding_curve":
            if phase != "bonding":
                add("bonding_curve_new_launch", 8)
        elif kind == "clmm_only":
            add("clmm_liquidity_pullable", 20)
            if crit.get("veto_clmm_only", True):
                vetoes.append("lp_pullable_clmm_only")
        else:
            add("uncertainty:no_lp_data", 15); unc.append("no_lp_data")
        # holders
        t10, t1 = holders(report)
        if t10 is None:
            add("uncertainty:no_holder_data", 15); unc.append("no_holder_data")
        else:
            comp["top10_pct"], comp["top1_pct"] = round(t10, 1), round(t1, 1)
            add("top10_concentration", 20 if t10 > 30 else 10 if t10 > 20 else 4 if t10 > 15 else 0)
            add("single_holder", 15 if t1 > 10 else 6 if t1 > 5 else 0)
            if t10 > crit["max_top10_pct"] or t1 > crit["max_single_pct"]:
                vetoes.append(f"extreme_holder_concentration(top10={t10:.0f}%,top1={t1:.0f}%)")
        ins = report.get("graphInsidersDetected") or 0
        add("insider_network", 0 if ins == 0 else 8 if ins <= 5 else 15 if ins <= 50 else 22)
        n = report.get("totalHolders") or 0
        add("few_holders", 10 if n < 100 else 4 if n < 300 else 0)
        sn = report.get("score_normalised") or 0
        add("rugcheck_score", min(15, sn * 0.3))
        dangers = [r["name"] for r in report.get("risks") or [] if r.get("level") == "danger"]
        if any("rugged" in d.lower() or "creator history" in d.lower() for d in dangers):
            add("dev_rug_history", 20)
        add("rugcheck_danger_flags", min(15, 5 * len(dangers)))
        if (report.get("tokenMeta") or {}).get("mutable"):
            add("mutable_metadata", 2)
    # sell route / exit price impact
    if route is None:
        add("uncertainty:no_route_check", 20); unc.append("route_not_checked")
    elif not route.get("sell_ok"):
        vetoes.append("no_sell_route(honeypot_risk)")
    else:
        rt = route["round_trip_loss_pct"]
        comp["round_trip_loss_pct"] = round(rt, 2)
        add("exit_impact", 0 if rt < 2 else 5 if rt < 4 else 12 if rt < 8 else 25)
        if rt > crit["max_round_trip_loss_pct"]:
            vetoes.append(f"exit_impact_too_high({rt:.1f}%)")
    return min(100, round(pts)), vetoes, comp, unc


def band(s):
    return "low" if s <= 20 else "moderate" if s <= 40 else "elevated" if s <= 60 else "high" if s <= 80 else "critical"


def pump_score(pair, report, route, crit, pump):
    """pump.fun-adapted Rug Risk Score. Starts from the generic score (authorities, Token-2022, LP for graduated pools,
    holder concentration excluding the bonding curve / pool accounts, insiders, exit impact) and adds pump-specific
    risk: dev holdings and dev selling, same-block bundle buys at launch, dev's past launches (serial launchers),
    bonding-curve progress and pump.fun's own moderation flags. Missing pump data = uncertainty penalty."""
    s, vetoes, comp, unc = score(pair, report, route, crit, pump.get("phase"))
    pts = s

    def add(name, p):
        nonlocal pts
        if p:
            comp[name] = round(p, 1)
            pts += p

    phase = pump.get("phase")
    comp["phase"] = phase
    coin = pump.get("coin") or {}
    if coin.get("is_banned"):
        vetoes.append("pumpfun_banned")
    sv = (coin.get("security_verdict") or {}).get("verdict")
    if sv and sv != "allow":
        vetoes.append(f"pumpfun_security_verdict_{sv}")
    if coin.get("transfer_hook_program"):
        vetoes.append("transfer_hook_program")
    if (coin.get("transfer_fee_bps") or 0) > 0:
        vetoes.append(f"transfer_fee_{coin['transfer_fee_bps']}bps")
    # dev history
    dh = pump.get("dev_history")
    if report and any("creator history" in (r.get("name") or "").lower() for r in report.get("risks") or []):
        vetoes.append("serial_rugger(rugcheck_creator_history)")
    if dh is None:
        add("uncertainty:no_dev_history", 8); unc.append("no_dev_history")
    else:
        comp["dev_coins"], comp["dev_graduated"] = dh["coins"], dh["graduated"]
        if dh["coins"] >= crit.get("serial_launcher_coins", 20) and dh["graduated"] == 0:
            vetoes.append(f"serial_launcher({dh['coins']}{'+' if dh['capped_at_50'] else ''}_coins_0_graduated)")
        elif dh["coins"] >= 5 and dh["graduated"] == 0:
            add("dev_many_failed_launches", 15)
        elif dh["coins"] >= 5 and dh["graduated"] / dh["coins"] < 0.1:
            add("dev_low_graduation_rate", 8)
    # launch analysis
    la = pump.get("launch") or {}
    if not la.get("trades_fetched"):
        add("uncertainty:no_trade_history", 12); unc.append("no_pump_trade_history")
    else:
        dev_net = (la.get("dev_bought_pct_supply") or 0) * (1 - (la.get("dev_sold_pct_of_buys") or 0) / 100)
        comp["dev_hold_est_pct"] = round(dev_net, 2)
        add("dev_holdings", 20 if dev_net > 10 else 10 if dev_net > 5 else 4 if dev_net > 2 else 0)
        if dev_net > crit.get("max_dev_hold_pct", 20):
            vetoes.append(f"dev_holds_{dev_net:.0f}%")
        if (la.get("dev_sold_pct_of_buys") or 0) >= 50 and (pump.get("age_min") or 999) < 60:
            add("dev_dumping_early", 10)
        if la.get("reached_launch"):
            b = la.get("bundle_pct_supply") or 0
            comp["bundle_pct"] = b
            add("launch_bundle", 25 if b > 20 else 15 if b > 10 else 7 if b > 5 else 0)
            if b > crit.get("max_bundle_pct", 40):
                vetoes.append(f"heavy_launch_bundle({b:.0f}%)")
        else:
            add("uncertainty:launch_not_reached", 5 if phase == "graduated" else 10); unc.append("launch_trades_not_reached")
    # bonding curve progress
    prog = pump.get("progress")
    if phase == "bonding":
        comp["curve_progress"] = prog
        if prog is None:
            add("uncertainty:no_curve_progress", 8)
        else:
            add("curve_progress", 10 if prog < 10 else 5 if prog < 25 else 3 if prog > 95 else 0)
    return min(100, round(pts)), vetoes, comp, unc
