#!/usr/bin/env bash
# Cutover from the box to GitHub Actions (PAPER ONLY). Run on the box right before the state blob is pushed.
# Order matters: the supervisor would restart the bot (and it SIGINTs, which the nohup'd bot ignores), so it is stopped first
# with SIGKILL (it holds no state). The bots get SIGTERM: main bot closes its paper positions and writes its report,
# M4 and S5 save their open positions for GitHub to continue. Then the encrypted state is exported.
set -u
BOT=/workspace/solana-bot; APP=/workspace/sentinel-app
pids() { pgrep -f "$1" || true; }
waitgone() { for i in $(seq 1 ${2:-150}); do kill -0 "$1" 2>/dev/null || return 0; sleep 1; done; echo "WARN: $1 still running"; return 1; }
for p in $(pids "/workspace/sentinel-app/supervisor.py"); do echo "stop supervisor $p"; kill -KILL "$p"; done
for p in $(pids "/workspace/sentinel-app/m4_watchdog.py"); do echo "stop m4 watchdog $p"; kill -KILL "$p"; done
for p in $(pids "m4_momentum.py"); do echo "SIGTERM m4_momentum $p"; kill -TERM "$p"; waitgone "$p" 60; done
for p in $(pids "/workspace/sentinel-app/s5_watchdog.py"); do echo "stop s5 watchdog $p"; kill -KILL "$p"; done
for p in $(pids "s5_quant.py"); do echo "SIGTERM s5_quant $p"; kill -TERM "$p"; waitgone "$p" 60; done
for p in $(pids "pump_sentinel.py"); do echo "SIGTERM pump_sentinel $p (closes paper positions, writes report)"; kill -TERM "$p"; waitgone "$p" 180; done
sleep 3
for p in $(pids "relay_publisher.py"); do echo "SIGTERM relay $p"; kill -TERM "$p"; waitgone "$p" 20; done
for p in $(pids "uvicorn server:app --host 127.0.0.1 --port 8787"); do echo "SIGTERM server $p"; kill -TERM "$p"; waitgone "$p" 20; done
python3 "$(dirname "$0")/export_local_state.py" --bot-dir "$BOT" --app-dir "$APP" --out /workspace/sentinel-state-export.bin
echo "Now push /workspace/sentinel-state-export.bin to the orphan branch 'state' as state.bin and dispatch the workflow."
