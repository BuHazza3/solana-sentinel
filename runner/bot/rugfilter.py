"""Rug / risk filter. Returns (passed: bool, reasons: list[str], info: dict)."""
SYSTEM = "11111111111111111111111111111111"


def check(pair, report, cfg):
    f = cfg["filter"]
    reasons, info = [], {}
    liq = ((pair or {}).get("liquidity") or {}).get("usd") or 0
    info["liq"] = liq
    if liq < f["min_liquidity_usd"]:
        reasons.append(f"low_liquidity(<{f['min_liquidity_usd']})")
    if (pair or {}).get("fdv") and pair["fdv"] > f.get("max_fdv_usd", 1e18):
        reasons.append("fdv_too_high")
    if not report:
        if f.get("require_rugcheck", True):
            reasons.append("rugcheck_unavailable")
        return (not reasons), reasons, info
    if report.get("rugged"):
        reasons.append("rugcheck_marked_rugged")
    if report.get("mintAuthority"):
        reasons.append("mint_authority_enabled")
    if report.get("freezeAuthority"):
        reasons.append("freeze_authority_enabled")
    tf = (report.get("transferFee") or {}).get("pct") or 0
    if tf > 0:
        reasons.append("transfer_fee")
    sn = report.get("score_normalised")
    info["rc_score"] = sn
    if sn is not None and sn > f["max_rugcheck_score_normalised"]:
        reasons.append(f"rugcheck_score_high({sn})")
    dangers = [r["name"] for r in report.get("risks") or [] if r.get("level") == "danger"
               and r["name"] not in f.get("allowed_danger_risks", [])]
    if dangers and f.get("reject_any_danger_risk", True):
        reasons.append("rugcheck_danger:" + "|".join(dangers)[:80])
    holders = report.get("totalHolders") or 0
    info["holders"] = holders
    if holders < f["min_holders"]:
        reasons.append(f"few_holders({holders})")
    ins = report.get("graphInsidersDetected") or 0
    if ins > f["max_insiders_detected"]:
        reasons.append(f"insiders_detected({ins})")

    # ---- LP lock / burn ----
    markets = report.get("markets") or []
    lp_markets = [m for m in markets if (m.get("lp") or {}).get("lpMint") not in (None, SYSTEM)]
    if lp_markets:
        tot = sum((m["lp"].get("quoteUSD") or 0) + (m["lp"].get("baseUSD") or 0) for m in lp_markets) or 1
        locked = sum(((m["lp"].get("quoteUSD") or 0) + (m["lp"].get("baseUSD") or 0)) * (m["lp"].get("lpLockedPct") or 0)
                     for m in lp_markets) / tot
        info["lp_locked_pct"] = round(locked, 1)
        if locked < f["min_lp_locked_pct"]:
            reasons.append(f"lp_unlocked({locked:.0f}%)")
    elif any(m.get("marketType") == "pump_fun" for m in markets):
        info["lp_locked_pct"] = "bonding_curve"  # pump.fun curve: liquidity cannot be pulled
    elif markets:
        info["lp_locked_pct"] = "clmm_only"
        if not f.get("allow_clmm_only_liquidity", False):
            reasons.append("only_concentrated_liquidity(LP_pullable)")

    # ---- holder concentration (excluding AMM/pool/known program accounts) ----
    exclude = set((report.get("knownAccounts") or {}).keys())
    for m in markets:
        exclude |= {m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB")}
        for k in ("liquidityAAccount", "liquidityBAccount"):
            if m.get(k):
                exclude.add(m[k].get("owner"))
    hs = [h for h in report.get("topHolders") or [] if h.get("owner") not in exclude and h.get("address") not in exclude]
    top10 = sum(h.get("pct") or 0 for h in hs[:10])
    top1 = max([h.get("pct") or 0 for h in hs] or [0])
    info["top10_pct"], info["top1_pct"] = round(top10, 1), round(top1, 1)
    if top10 > f["max_top10_holder_pct"]:
        reasons.append(f"top10_concentration({top10:.0f}%)")
    if top1 > f["max_single_holder_pct"]:
        reasons.append(f"single_holder({top1:.0f}%)")
    return (not reasons), reasons, info
