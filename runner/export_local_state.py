#!/usr/bin/env python3
"""Export the local (box) bot state as the encrypted blob the GitHub runner restores (PAPER ONLY).
Run AFTER the local bots are stopped (see cutover_local.sh). Reads the key from a file, never prints it.
usage: python3 export_local_state.py [--bot-dir /workspace/solana-bot] [--app-dir /workspace/sentinel-app]
                                     [--key-file /workspace/sentinel-app/.relay_topic] [--out /workspace/sentinel-state-export.bin]"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import statestore

ap = argparse.ArgumentParser()
ap.add_argument("--bot-dir", default="/workspace/solana-bot")
ap.add_argument("--app-dir", default="/workspace/sentinel-app")
ap.add_argument("--key-file", default="/workspace/sentinel-app/.relay_topic")
ap.add_argument("--out", default="/workspace/sentinel-state-export.bin")
a = ap.parse_args()
key = open(a.key_file).read().strip()
raw = statestore.pack(a.bot_dir, a.app_dir)
blob = statestore.seal(raw, key)
tmp = a.out + ".tmp"
open(tmp, "wb").write(blob)
os.replace(tmp, a.out)
# self-check: decrypts and lists the two bots' accounts
import io, tarfile
names = tarfile.open(fileobj=io.BytesIO(statestore.unseal(blob, key)), mode="r:gz").getnames()
acc = {b: (json.load(open(os.path.join(a.bot_dir, "data", f"{b}_bot_account.json"))) if os.path.exists(os.path.join(a.bot_dir, "data", f"{b}_bot_account.json")) else {})
       for b in ("v1", "s5")}
print(f"wrote {a.out}: {len(blob) // 1024} KB, {len(names)} files, V1 open positions: {len(acc['v1'].get('positions', {}))}, "
      f"S5 open positions: {len(acc['s5'].get('positions', {}))}, switches saved: {'bot/data/agent_toggles.json' in names}")
