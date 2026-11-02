"""Encrypted state for the 24/7 paper-bot runner. PAPER ONLY: the state holds paper balances, positions and history.
Package = gzip tar of the bot data + app history, encrypted with AES-256-GCM using a key derived from RELAY_KEY
(SHA-256 of "sentinel-state:" + key; different from the relay's own keys). The repo is public, so only the encrypted blob
is ever stored (orphan branch `state`, file `state.bin`). Nothing here prints the key."""
import glob, hashlib, io, json, os, secrets, sqlite3, subprocess, tarfile, tempfile, time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC, AAD = b"ST1", b"sentinel-state"
KEEP_DAYS_M4_SKIPS = 3


def _aes(secret):
    return AESGCM(hashlib.sha256(("sentinel-state:" + secret).encode()).digest())


def seal(raw, secret):
    iv = secrets.token_bytes(12)
    return MAGIC + iv + _aes(secret).encrypt(iv, raw, AAD)


def unseal(blob, secret):
    if blob[:3] != MAGIC:
        raise ValueError("not a sentinel state blob")
    return _aes(secret).decrypt(blob[3:15], blob[15:], AAD)


def _db_copy(src, dst, prune_sql=()):
    """Consistent copy of a live SQLite db (online backup API), then prune old rows and compact."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(dst)
    s.backup(d)
    s.close()
    for q, args in prune_sql:
        try:
            d.execute(q, args)
        except sqlite3.OperationalError:
            pass
    d.commit()
    d.execute("VACUUM")
    d.close()


def _current_main_run(data):
    f = os.path.join(data, "pump_run.json")
    if os.path.exists(f):
        try:
            return json.load(open(f))["run"]
        except Exception:
            pass
    logs = sorted(glob.glob(os.path.join(data, "pump_sentinel_P*.log")), key=os.path.getmtime)
    return os.path.basename(logs[-1])[len("pump_sentinel_"):-4] if logs else None


def pack(bot_dir, app_dir):
    """Returns gzip-tar bytes of everything needed to continue: accounts, open positions, history, app event history."""
    data = os.path.join(bot_dir, "data")
    run = _current_main_run(data)
    m4run = open(os.path.join(data, "m4_run.txt")).read().strip() if os.path.exists(os.path.join(data, "m4_run.txt")) else None
    s5run = open(os.path.join(data, "s5_run.txt")).read().strip() if os.path.exists(os.path.join(data, "s5_run.txt")) else None
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(path, arc):
            if os.path.exists(path):
                tar.add(path, arcname=arc)
        names = ["pump_run.json", "pump_accounts.json", "pump_trades.csv", "m4_account.json", "m4_run.txt", "m4_trades.csv", "m4_state.json",
                 "s5_account.json", "s5_run.txt", "s5_trades.csv", "s5_state.json"]
        if run: names.append(f"pump_sentinel_{run}.log")
        if m4run: names.append(f"m4_momentum_{m4run}.log")
        if s5run: names.append(f"s5_quant_{s5run}.log")
        names += [os.path.basename(p) for p in sorted(glob.glob(os.path.join(data, "pump_report_P*.txt")))[-10:]]
        for n in names:
            add(os.path.join(data, n), f"bot/data/{n}")
        cut = time.time() - KEEP_DAYS_M4_SKIPS * 86400
        for name, prune in (("pump_sentinel.db", [("DELETE FROM decisions WHERE run != ?", (run,)), ("DELETE FROM scans WHERE run != ?", (run,)),
                                                  ("DELETE FROM regime WHERE run != ?", (run,))]),
                            ("m4_momentum.db", [("DELETE FROM decisions WHERE approved = 0 AND ts < ?", (cut,))]),
                            ("s5_quant.db", [("DELETE FROM decisions WHERE approved = 0 AND ts < ?", (cut,))])):
            src = os.path.join(data, name)
            if os.path.exists(src):
                dst = os.path.join(tmp, name)
                _db_copy(src, dst, prune if run or name != "pump_sentinel.db" else ())
                tar.add(dst, arcname=f"bot/data/{name}")
        src = os.path.join(app_dir, "app.db")
        if os.path.exists(src):
            dst = os.path.join(tmp, "app.db")
            _db_copy(src, dst, [("DELETE FROM ev WHERE run != ? AND run NOT LIKE 'M%'", (run,)), ("DELETE FROM off", ())])
            tar.add(dst, arcname="app/app.db")
        meta = os.path.join(tmp, "meta.json")
        json.dump({"saved": time.time(), "main_run": run, "m4_run": m4run, "s5_run": s5run, "format": 1}, open(meta, "w"))
        tar.add(meta, arcname="meta.json")
    return buf.getvalue()


def unpack(raw, bot_dir, app_dir):
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or ".." in m.name or m.name.startswith("/"):
                continue
            if m.name.startswith("bot/data/"):
                dst = os.path.join(bot_dir, "data", m.name[len("bot/data/"):])
            elif m.name.startswith("app/"):
                dst = os.path.join(app_dir, m.name[len("app/"):])
            elif m.name == "meta.json":
                dst = os.path.join(bot_dir, "data", "state_meta.json")
            else:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with tar.extractfile(m) as f, open(dst, "wb") as out:
                out.write(f.read())


# ---------------------------------------------------------------- storage: orphan branch `state` (never touches main / Pages)
def _git(repo, *a, inp=None, env=None):
    return subprocess.run(["git", "-C", repo, *a], input=inp, capture_output=True, check=True, env=env).stdout


def git_load(repo, branch="state", path="state.bin"):
    try:
        _git(repo, "fetch", "--quiet", "--depth=1", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    except subprocess.CalledProcessError:
        return None   # branch doesn't exist yet
    try:
        return _git(repo, "show", f"origin/{branch}:{path}")
    except subprocess.CalledProcessError:
        return None


def git_save(repo, blob, branch="state", path="state.bin"):
    """Single-commit orphan branch, force-pushed (keeps the repo small; no working-tree changes, no Pages rebuild)."""
    env = dict(os.environ, GIT_AUTHOR_NAME="sentinel-runner", GIT_AUTHOR_EMAIL="runner@users.noreply.github.com",
               GIT_COMMITTER_NAME="sentinel-runner", GIT_COMMITTER_EMAIL="runner@users.noreply.github.com")
    sha = _git(repo, "hash-object", "-w", "--stdin", inp=blob).decode().strip()
    readme = _git(repo, "hash-object", "-w", "--stdin",
                  inp=b"Encrypted paper-bot state (AES-256-GCM). Written by runner/runner.py. Do not edit.\n").decode().strip()
    tree = _git(repo, "mktree", inp=f"100644 blob {readme}\tREADME.md\n100644 blob {sha}\t{path}\n".encode()).decode().strip()
    commit = _git(repo, "commit-tree", tree, "-m", f"state {time.strftime('%Y-%m-%d %H:%M:%S')}", env=env).decode().strip()
    _git(repo, "push", "--quiet", "--force", "origin", f"{commit}:refs/heads/{branch}")
    return commit
