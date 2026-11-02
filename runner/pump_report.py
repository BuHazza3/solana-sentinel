"""Text summary for a pump.fun sentinel run, split by bonding-curve vs graduated bucket. Real numbers only."""
import statistics, time, collections


def write_report(run, accts, glob, sol0, sol1, t0, t1, src_stats, cfg):
    L = []
    p = L.append
    p(f"PUMP.FUN SENTINEL PAPER RUN {run}   duration {(t1 - t0) / 60:.1f} min   "
      f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(t0))} to {time.strftime('%H:%M', time.localtime(t1))} UTC+4 Dubai)")
    p(f"SOL price: start ${sol0:.2f} -> end ${sol1:.2f} ({(sol1 / sol0 - 1) * 100:+.2f}%)")
    p("")
    p(f"BASELINES (start 1 SOL = ${sol0:.2f})")
    p(f"  HOLD_SOL : 1.0000 SOL = ${sol1:.2f}  ({(sol1 / sol0 - 1) * 100:+.2f}% in USD)")
    p(f"  NO_TRADE : ${sol0:.2f} cash  (0.00% in USD; {(sol0 / sol1 - 1) * 100:+.2f}% in SOL terms)")
    p("")
    p("ACCOUNTS (each 1 SOL; open positions were sold at the end with a real Jupiter quote, so all PnL is realized and net of costs)")
    for n, a in accts.items():
        eq = a.equity()
        p(f"== {n}: end {eq:.5f} SOL (${eq * sol1:.2f}) | net {eq - a.start:+.5f} SOL ({(eq / a.start - 1) * 100:+.2f}%) | "
          f"vs HOLD_SOL {(eq - 1) * sol1:+.2f} USD | vs NO_TRADE {eq * sol1 - sol0:+.2f} USD | closed {len(a.closed)}"
          f"{' | HALTED: ' + a.halted if a.halted else ''}")
        by_phase = collections.defaultdict(list)
        for t in a.closed:
            by_phase[t["phase"]].append(t)
        for ph, tr in by_phase.items():
            costs = [((t["fees"] + t["fees"]) / 2 + 0) for t in tr]
            p(f"   bucket {ph}: {len(tr)} trades, PnL {sum(t['pnl_sol'] for t in tr):+.5f} SOL, wins {sum(1 for t in tr if t['pnl_sol'] > 0)}")
        for key in sorted(a.stats):
            st = a.stats[key]
            s, ph = key.split(":")
            tr = [t for t in a.closed if t["strategy"] == s and t["phase"] == ph]
            cs = a.cost_samples.get(key) or []
            p(f"   [{key}] signals {st['signals']} | rejected {st['rejected']} | approved {st['approved']} | closed {len(tr)} | "
              f"wins {sum(1 for t in tr if t['pnl_sol'] > 0)} | PnL {sum(t['pnl_sol'] for t in tr):+.5f} SOL"
              + (f" | avg round-trip cost {statistics.mean(cs):.2f}% of position" if cs else ""))
            for r, c in a.rejects[key].most_common(8):
                p(f"        {c:4}  {r}")
            for t in tr:
                p(f"        trade {t['symbol'][:10]:10} size {t['size_sol']:.4f} pnl {t['pnl_sol']:+.5f} ({t['pnl_pct']:+.1f}%) exit={t['exit_reason']} "
                  f"hold={t['closed'] - t['opened']:.0f}s rug={t['rug_score']} pool_fee={t['pool_fee_pct']}% est_cost={t['cost_pct_est'] and round(t['cost_pct_est'], 2)}% "
                  f"wallets={t['wallets']} src={t['discovered_by']}")
    p("")
    p("SCANNER TOTALS")
    for k, v in sorted(glob.items()):
        p(f"  {k}: {v}")
    p("DATA SOURCES (ok/fail)")
    for k, v in src_stats.items():
        p(f"  {k}: ok={v['ok']} fail={v['fail']} {v['last_error']}")
    txt = "\n".join(L)
    open(f"data/pump_report_{run}.txt", "w").write(txt)
    print("\n" + txt)
