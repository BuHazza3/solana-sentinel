"""Rank wallets by realized PnL computed from their recent on-chain swaps (public RPC, read-only).

Realized PnL per token = SOL received from sells - SOL spent on buys, counted only for tokens whose
position was (nearly) fully exited inside the sampled window. Fees paid by the wallet are included.
Limitations: only the last N transactions are sampled; tokens bought before the window are ignored
(sells without buys are skipped), and non-SOL-quoted swaps are skipped.
"""
import statistics, time
from concurrent.futures import ThreadPoolExecutor
from .solana_rpc import RPC, extract_swap


def analyze_wallet(rpc, wallet, max_txs=150, log=print):
    sigs = []
    before = None
    while len(sigs) < max_txs:
        batch = rpc.signatures(wallet, limit=min(100, max_txs - len(sigs)), before=before)
        if not batch:
            break
        sigs += batch
        before = batch[-1]["signature"]
        if len(batch) < 100:
            break
    ok_sigs = [s for s in sigs if not s.get("err")]
    span_sigs = (sigs[0].get("blockTime", 0) - sigs[-1].get("blockTime", 0)) if len(sigs) > 1 else 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        txs = list(ex.map(lambda s: rpc.tx(s["signature"]), ok_sigs))
    swaps = [sw for sw in (extract_swap(t, wallet) for t in txs) if sw]
    swaps.sort(key=lambda x: x["ts"] or 0)
    pos = {}
    for sw in swaps:
        p = pos.setdefault(sw["mint"], {"bought": 0, "sold": 0, "sol_in": 0, "sol_out": 0, "t0": None, "t1": None})
        if sw["side"] == "buy":
            p["bought"] += sw["tokens"]; p["sol_in"] += sw["sol"]; p["t0"] = p["t0"] or sw["ts"]
        else:
            if p["bought"] == 0:
                continue  # entry happened before window
            p["sold"] += sw["tokens"]; p["sol_out"] += sw["sol"]; p["t1"] = sw["ts"]
    closed = {m: p for m, p in pos.items() if p["bought"] > 0 and p["sold"] >= 0.9 * p["bought"]}
    pump_closed = {m: p for m, p in closed.items() if m.endswith("pump")}
    pump_pnl = [p["sol_out"] - p["sol_in"] for p in pump_closed.values()]
    pnl = [p["sol_out"] - p["sol_in"] for p in closed.values()]
    holds = [(p["t1"] - p["t0"]) for p in closed.values() if p["t0"] and p["t1"]]
    span_h = ((swaps[-1]["ts"] - swaps[0]["ts"]) / 3600) if len(swaps) > 1 else 0
    return {
        "wallet": wallet,
        "txs_sampled": len(sigs),
        "sig_window_hours": round(span_sigs / 3600, 2),
        "swaps": len(swaps),
        "window_hours": round(span_h, 2),
        "closed_tokens": len(closed),
        "wins": sum(1 for x in pnl if x > 0),
        "win_rate": round(sum(1 for x in pnl if x > 0) / len(pnl), 3) if pnl else None,
        "realized_pnl_sol": round(sum(pnl), 3),
        "sol_volume": round(sum(p["sol_in"] for p in closed.values()), 3),
        "median_hold_s": int(statistics.median(holds)) if holds else None,
        "swaps_per_hour": round(len(swaps) / span_h, 1) if span_h else None,
        "pump_closed_tokens": len(pump_closed),
        "pump_realized_pnl_sol": round(sum(pump_pnl), 3),
        "pump_win_rate": round(sum(1 for x in pump_pnl if x > 0) / len(pump_pnl), 3) if pump_pnl else None,
        "pump_share_of_closed": round(len(pump_closed) / len(closed), 2) if closed else None,
        "open_tokens": len([m for m, p in pos.items() if p["bought"] > 0 and m not in closed]),
    }


def bot_flags(r):
    flags = []
    if r["swaps_per_hour"] and r["swaps_per_hour"] > 120:
        flags.append("very high frequency (likely bot)")
    if r["median_hold_s"] is not None and r["median_hold_s"] < 15:
        flags.append("median hold <15s (sniper/bot-like, hard to copy)")
    if r.get("sig_window_hours", 1) < 0.5:
        flags.append("history flooded by spam/dust mentions; public RPC sample covers <30min, cannot verify")
    if r["closed_tokens"] < 3:
        flags.append("too few closed trades in window")
    return flags
