# 24/7 runner (GitHub Actions) — PAPER ONLY

Runs the paper bots (no wallets, no keys, no real transactions) on GitHub Actions so they keep going when the box sleeps.

- `.github/workflows/bots.yml` starts `runner/runner.py`. One job runs ~5h40m, then stops the bots with SIGTERM (open paper
  positions are saved, not sold), saves state and dispatches the next run. A cron every 30 min (at :07 and :37) is a fallback
  and exits at once if a run is active. Only one bots job runs at a time (concurrency group `paper-bots`).
- Inside the job: two multi-agent paper bots, `agent_bot.py --bot V1` (V1 Sniper) and `agent_bot.py --bot S5` (S5 Quant). Each has
  ONE shared 1 SOL paper bankroll with its own risk limits (`v1_bot.toml`, `s5_bot.toml`) and runs sub-agents from `agents.toml`
  (V1 snipe, V1 scanner, copy-trade, C momentum (graduated), launch sniper, M4 momentum scan, and in S5 also Revival); every
  sub-agent keeps its original entry rules and exits (`agent_exits.py`). The copy-trade watch list is `wallets.toml` (addresses
  to watch only; nothing is ever connected). Plus the headless app server on 127.0.0.1 (`app/server.py` + `app/bots_view.py`)
  and `app/relay_publisher.py` (same encrypted MQTT + ntfy format and topic derivation, so the existing private app link keeps working).
- On/off switches: one per sub-agent plus a master switch per bot, in the app's Agents tab (phone app only). A tap publishes an
  AES-256-GCM command on a separate topic with a separately derived key (`sha256("sentinel-cmd-key:" + RELAY_KEY)`, AAD
  `sentinel-cmd`); `app/toggle_cmd.py` in the relay process accepts it only if it decrypts/authenticates, names a known switch,
  is within ±120 s, is newer than the last command for that switch and its nonce is unused. It then writes
  `bot/data/agent_toggles.json`, which the bots re-read every 2 s. Off = no new entries; open positions still exit normally.
  The switches are part of the saved state, so they survive handovers. Every command is logged (`app/logs/toggle_commands.log`).
  There are no trade, wallet or deposit actions anywhere.
- Secret: only the Actions secret `RELAY_KEY` (the part after `#k=` in the private link). It is masked in the logs and written
  only to a 0600 file inside the job's temp dir. Never commit it.
- State: `statestore.py` packs both bots' bankrolls, open positions, history, run ids and switches, encrypts them (AES-256-GCM, key derived from
  RELAY_KEY) and force-pushes one commit to the orphan branch `state` (`state.bin`). `main` is never written (no Pages rebuild).
- Until `state.bin` exists on the `state` branch the workflow does nothing (unless dispatched with `fresh`).
- Cutover from the box: `runner/cutover_local.sh` (stops the local supervisor, watchdogs and bots with SIGTERM and exports `/workspace/sentinel-state-export.bin`),
  then push that file as `state.bin` to the `state` branch and dispatch the workflow.
- Stop everything: disable the workflow (Actions > paper-bots > ... > Disable workflow) and cancel the running job
  (cancel = SIGTERM: positions are saved, no new run is dispatched).

- Retired (2026-11-02): `pump_sentinel.py` (ORIGINAL / REVISED / V1_SNIPER), `m4_momentum.py` (M4) and `s5_quant.py`. Their
  sub-agents that made profitable paper trades now run inside V1 and S5; their history is summarised in `bot/data/legacy_agents.json`.
- Tests: `python3 -m pytest -q runner/tests`.
