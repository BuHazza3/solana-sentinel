# S7 Copy Elite + v9 app (paper mode only)

Base: BuHazza3/solana-sentinel main @ 47b7cd1 (v8.3). Extract at the repo root, commit, push to main. No wallets, no keys, no new dependencies.
The workflow (`.github/workflows/bots.yml`) is unchanged: `runner/runner.py` now also starts `agent_bot.py --bot S7`.

## What it adds
- Bot 4, **S7 "S7 Copy Elite"** (`runner/s7_bot.toml`): its own 1 SOL paper bankroll, **copy-trade only**.
  - Agents (both ON by default, each with its own switch + the S7 master switch):
    - **S7_TOP** copies tier-1 wallets: 10% of cash, max 0.10 SOL.
    - **S7_NEXT** copies tier-2 wallets: 6% of cash, max 0.06 SOL. Min trade 0.02 SOL, 0.05 SOL kept.
  - Entry filters: V1 rug filter, pump.fun mints only, market cap $5K–$1M, liquidity ≥ $5K, one position per token.
  - Exits (card engine): stop −32% before 2x; 55% sold at 2x (stake back), the rest rides a 30% trail from its peak and is all sold at 5x
    or after 120 min; out when the copied wallet sells before 2x; out at 45 min if it never reached 1.25x.
  - Max 4 open; daily auto-stop −10% (resumes 00:00 Dubai).
- **Wallet ranking** (`runner/s7_rank.py`, seed `runner/s7_seed.json`), re-computed on every copy signal:
  - Score = each wallet's last 20 results: replay of its past buys with S7's exits (seed) + S7's own closed trades.
  - Copied only with ≥ 3 results, net > 0 and profit factor ≥ 1. Tier 1 = the best 2 with ≥ 5 results and PF ≥ 1.5; tier 2 = the rest of the best 5.
  - Dropped after 3 S7 losses in a row, or a negative net over its last 5 S7 trades.
  - The app's S7 → Agents tab shows the ranking table and each wallet's tier.
- `s5_core.Position`: optional card keys `stale_min` / `stale_x` (agents.toml `no_lift_minutes` / `no_lift_x`). Old cards are unchanged.
- App v9: S7 tab, ranking card, S7 rules; relay trimming covers S7 automatically (smoke: 55,190-byte MQTT payload at trim level 1, cap 60,000).

## Tests
`python -m pytest runner/tests`: 71 passed (52 existing + 19 new in test_s7.py).
