#!/usr/bin/env bash
# Cutover from the box to GitHub Actions (PAPER ONLY). Run on the box right before the state blob is pushed.
# logs/HOLD stops the supervisor and the bot watchdogs from restarting each other; then everything gets SIGTERM:
# the V1 and S5 bots save their open paper positions and switches for GitHub to continue. Then the encrypted state is exported.
set -u
BOT=/workspace/solana-bot; APP=/workspace/sentinel-app
pids() { pgrep -f "$1" || true; }
waitgone() { for i in $(seq 1 ${2:-150}); do kill -0 "$1" 2>/dev/null || return 0; sleep 1; done; echo "WARN: $1 still running"; return 1; }
touch "$APP/logs/HOLD"
for p in $(pids "/workspace/sentinel-app/supervisor.py"); do echo "SIGTERM supervisor $p (stops app + relay)"; kill -TERM "$p"; waitgone "$p" 40; done
for p in $(pids "bot_watchdog.py --bot"); do echo "SIGTERM bot watchdog $p"; kill -TERM "$p"; done
for p in $(pids "agent_bot.py --bot"); do echo "SIGTERM bot $p (keeps open paper positions)"; kill -TERM "$p"; waitgone "$p" 90; done
for p in $(pids "relay_publisher.py"); do echo "SIGTERM relay $p"; kill -TERM "$p"; waitgone "$p" 20; done
for p in $(pids "uvicorn server:app --host 127.0.0.1 --port 8787"); do echo "SIGTERM server $p"; kill -TERM "$p"; waitgone "$p" 20; done
python3 "$(dirname "$0")/export_local_state.py" --bot-dir "$BOT" --app-dir "$APP" --out /workspace/sentinel-state-export.bin
echo "Now push /workspace/sentinel-state-export.bin to the orphan branch 'state' as state.bin and dispatch the workflow."
echo "(To resume on the box instead: rm $APP/logs/HOLD && $APP/start.sh)"
