# 24/7 runner (GitHub Actions) — PAPER ONLY

Runs the paper bots (no wallets, no keys, no real transactions) on GitHub Actions so they keep going when the box sleeps.

- `.github/workflows/bots.yml` starts `runner/runner.py`. One job runs ~5h40m, then stops the bots with SIGTERM (open paper
  positions are saved, not sold), saves state and dispatches the next run. A cron every 30 min (at :07 and :37) is a fallback
  and exits at once if a run is active. Only one bots job runs at a time (concurrency group `paper-bots`).
- Inside the job: `pump_sentinel.py --resume --keep-open-on-exit` (ORIGINAL / REVISED / V1_SNIPER), `m4_momentum.py` (M4), `s5_quant.py` (S5 Quant, the fifth strategy),
  the headless app server on 127.0.0.1 (`app/server.py`, builds the same snapshot as the box) and `app/relay_publisher.py`
  (same encrypted MQTT + ntfy format and topic derivation, so the existing private app link keeps working).
- Secret: only the Actions secret `RELAY_KEY` (the part after `#k=` in the private link). It is masked in the logs and written
  only to a 0600 file inside the job's temp dir. Never commit it.
- State: `statestore.py` packs bankrolls, open positions, history and run ids, encrypts them (AES-256-GCM, key derived from
  RELAY_KEY) and force-pushes one commit to the orphan branch `state` (`state.bin`). `main` is never written (no Pages rebuild).
- Until `state.bin` exists on the `state` branch the workflow does nothing (unless dispatched with `fresh`).
- Cutover from the box: `runner/cutover_local.sh` (stops local bots with SIGTERM and exports `/workspace/sentinel-state-export.bin`),
  then push that file as `state.bin` to the `state` branch and dispatch the workflow.
- Stop everything: disable the workflow (Actions > paper-bots > ... > Disable workflow) and cancel the running job
  (cancel = SIGTERM: positions are saved, no new run is dispatched).
