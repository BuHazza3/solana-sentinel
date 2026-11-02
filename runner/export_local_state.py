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
# self-check: decrypts and contains the M4 account
import io, tarfile
names = tarfile.open(fileobj=io.BytesIO(statestore.unseal(blob, key)), mode="r:gz").getnames()
m4 = json.load(open(os.path.join(a.bot_dir, "data", "m4_account.json"))) if os.path.exists(os.path.join(a.bot_dir, "data", "m4_account.json")) else {}
s5 = json.load(open(os.path.join(a.bot_dir, "data", "s5_account.json"))) if os.path.exists(os.path.join(a.bot_dir, "data", "s5_account.json")) else {}
print(f"wrote {a.out}: {len(blob) // 1024} KB, {len(names)} files, M4 open positions: {len(m4.get('positions', {}))}, "
      f"S5 open positions: {len(s5.get('positions', {}))}, S5 files: {sum(1 for n in names if '/s5_' in n)}")
