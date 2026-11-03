#!/usr/bin/env python3
"""24/7 orchestrator for the PAPER-ONLY pump.fun bots on GitHub Actions (no wallets, no keys, no real transactions).

One job = restore encrypted state -> run server + relay publisher + the three multi-agent paper bots (agent_bot.py --bot V1,
--bot S5, --bot S6, --bot S7; each sub-agent has an on/off switch) headless on localhost -> save state every SAVE_EVERY s ->
at RUN_SECONDS: SIGTERM bots (they persist open positions and switches), final save, dispatch the next run.
The relay publisher also receives the app's authenticated on/off commands (toggle_cmd.py) and writes bot/data/agent_toggles.json,
which is part of the saved state, so switches survive handovers. Job stdout is deliberately minimal (public logs); child output goes to files in the work dir.

Env: RELAY_KEY (required, the #k= secret), RUN_SECONDS (default 20400 = 5h40m), SAVE_EVERY (900), WORK (work dir),
     STATE_BACKEND git|file, STATE_FILE (file backend), FRESH=true (allow start without saved state),
     DISPATCH=1 + GITHUB_TOKEN/GITHUB_REPOSITORY/WORKFLOW_FILE/GITHUB_REF_NAME (dispatch next run), APP_PORT (8787)."""
import json, os, secrets, shutil, signal, subprocess, sys, time
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import statestore  # noqa: E402
import paper_reset

KEY = os.environ.get("RELAY_KEY", "").strip()
GHA = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "20400"))
SAVE_EVERY = int(os.environ.get("SAVE_EVERY", "900"))
WORK = os.path.abspath(os.environ.get("WORK", os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "sentinel-work")))
BACKEND = os.environ.get("STATE_BACKEND", "git" if GHA else "file")
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(WORK, "..", "state.bin"))
REPO = os.environ.get("GITHUB_WORKSPACE", os.path.dirname(HERE))
APP_PORT = os.environ.get("APP_PORT", "8787")
BOT, APP = os.path.join(WORK, "bot"), os.path.join(WORK, "app")
STATUS_F = os.path.join(WORK, "runner_status.json")
LOGS = os.path.join(WORK, "logs")
T0 = time.time()
DEADLINE = T0 + RUN_SECONDS
STOP = {"why": None}
STATUS = {"host": "GitHub Actions" if GHA else os.environ.get("RUNNER_HOST", "local test"), "job_started": T0,
          "handover_at": DEADLINE, "run_id": os.environ.get("GITHUB_RUN_ID"), "run_number": os.environ.get("GITHUB_RUN_NUMBER"),
          "heartbeat": T0, "phase": "starting", "sources": {}, "state_saved_at": None, "restored_from": None, "restarts": {}}
PROBES = {   # public, read-only endpoints the bots depend on (datacenter IPs are sometimes blocked: shown in the app)
    "pump.fun": "https://frontend-api-v3.pump.fun/coins?offset=0&limit=1&sort=last_trade_timestamp&order=DESC&includeNsfw=false",
    "DexScreener": "https://api.dexscreener.com/token-profiles/latest/v1",
    "Jupiter": "https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112",
    "Jupiter charts": "https://datapi.jup.ag/v2/charts/So11111111111111111111111111111111111111112?interval=1_MINUTE&to=4102444800000&candles=1&type=price",
    "RugCheck": "https://api.rugcheck.xyz/v1/stats/new_tokens",
    "Solana RPC": "https://api.mainnet-beta.solana.com",
}


def say(msg):   # the ONLY stdout of the job: short, never contains the key/token
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def write_status():
    STATUS["heartbeat"] = time.time()
    tmp = STATUS_F + ".tmp"
    json.dump(STATUS, open(tmp, "w"))
    os.replace(tmp, STATUS_F)


def probe():
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) sentinel-paper-bot"
    for name, url in PROBES.items():
        t = time.time()
        ok, code = False, None
        for attempt in range(2):
            try:
                if name == "Solana RPC":
                    r = sess.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=10)
                else:
                    r = sess.get(url, timeout=10)
                code, ok = r.status_code, r.status_code == 200
                if ok:
                    break
            except requests.RequestException as e:
                code = type(e).__name__
            time.sleep(2)
        STATUS["sources"][name] = {"ok": ok, "code": code, "ms": int((time.time() - t) * 1000), "ts": time.time()}


# ------------------------------------------------------------------ setup
def prepare():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(LOGS)
    shutil.copytree(HERE, BOT, ignore=shutil.ignore_patterns("app", "__pycache__", "runner.py", "statestore.py", "*.md", "requirements.txt"))
    shutil.copytree(os.path.join(HERE, "app"), APP, ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(BOT, "data"), exist_ok=True)
    os.makedirs(os.path.join(APP, "logs"), exist_ok=True)
    token = secrets.token_urlsafe(24)   # local-only access token for the headless server (never leaves the job)
    if GHA:
        print(f"::add-mask::{token}", flush=True)
    for name, val in ((".relay_topic", KEY), (".token", token)):
        p = os.path.join(APP, name)
        with open(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(val)


def load_state():
    blob = None
    if BACKEND == "git":
        blob = statestore.git_load(REPO)
    elif os.path.exists(STATE_FILE):
        blob = open(STATE_FILE, "rb").read()
    if blob is None:
        return False
    statestore.unpack(statestore.unseal(blob, KEY), BOT, APP)
    meta = json.load(open(os.path.join(BOT, "data", "state_meta.json")))
    STATUS["restored_from"] = meta.get("saved")
    return True


def save_state(final=False):
    for i in range(3):
        try:
            blob = statestore.seal(statestore.pack(BOT, APP), KEY)
            if BACKEND == "git":
                statestore.git_save(REPO, blob)
            else:
                tmp = STATE_FILE + ".tmp"
                open(tmp, "wb").write(blob)
                os.replace(tmp, STATE_FILE)
            STATUS["state_saved_at"] = time.time()
            say(f"state saved ({len(blob) // 1024} KB){' [final]' if final else ''}")
            return True
        except Exception as e:
            say(f"state save failed ({type(e).__name__}), retry {i + 1}/3")
            time.sleep(10 * (i + 1))
    return False


# ------------------------------------------------------------------ processes
class Proc:
    def __init__(self, name, args, cwd, env=None, restart=True):
        self.name, self.args, self.cwd, self.env, self.restart = name, args, cwd, env, restart
        self.p, self.fails, self.next_try = None, 0, 0

    def start(self):
        out = open(os.path.join(LOGS, f"{self.name}.out"), "ab")
        env = dict(os.environ, BOT_DIR=BOT, RUNNER_STATUS=STATUS_F, APP_PORT=APP_PORT, PYTHONUNBUFFERED="1")
        env.pop("RELAY_KEY", None)   # children read the key from the 0600 file, never from env
        env.pop("GITHUB_TOKEN", None)
        env.update(self.env or {})
        self.p = subprocess.Popen(self.args, cwd=self.cwd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  env=env, start_new_session=True)
        self.started = time.time()

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def tick(self):
        if self.alive() or not self.restart or time.time() < self.next_try:
            return
        if self.p is not None:
            code = self.p.returncode
            self.fails = 0 if time.time() - self.started > 600 else self.fails + 1
            STATUS["restarts"][self.name] = STATUS["restarts"].get(self.name, 0) + 1
            say(f"{self.name} exited ({code}); restarting")
            self.next_try = time.time() + min(300, 5 * 2 ** self.fails)
            self.p = None
            return
        self.start()

    def stop(self, sig=signal.SIGTERM, wait=90):
        if not self.alive():
            return
        try:
            os.kill(self.p.pid, sig)
        except ProcessLookupError:
            return
        try:
            self.p.wait(wait)
        except subprocess.TimeoutExpired:
            say(f"{self.name} did not stop in {wait}s; killing")
            os.killpg(self.p.pid, signal.SIGKILL)
            self.p.wait(10)


def dispatch_next():
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if os.environ.get("DISPATCH") != "1" or not tok or not repo:
        say("dispatch skipped (not configured)")
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{os.environ.get('WORKFLOW_FILE', 'bots.yml')}/dispatches"
    for i in range(5):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                              json={"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": {"handover": "true"}}, timeout=20)
            if r.status_code == 204:
                say("next run dispatched")
                return
            say(f"dispatch HTTP {r.status_code}")
        except requests.RequestException as e:
            say(f"dispatch error {type(e).__name__}")
        time.sleep(15 * (i + 1))
    say("dispatch failed; the 30-min cron will start the next run")


def main():
    if not KEY:
        say("RELAY_KEY is not set; nothing to do")
        return 0
    if GHA:
        print(f"::add-mask::{KEY}", flush=True)
    prepare()
    restored = load_state()
    if not restored and os.environ.get("FRESH", "").lower() != "true":
        say("no saved state yet (cutover not done) and FRESH is not true: exiting without starting the bots")
        return 0
    say("state restored" if restored else "starting fresh (no saved state)")
    try:                                     # one-time paper bankroll resets (runner/paper_resets.toml), idempotent across handovers
        paper_reset.apply_resets(BOT, os.path.join(HERE, "paper_resets.toml"), log=say)
    except Exception as e:
        say(f"paper reset skipped: {type(e).__name__}")
    write_status()

    py = sys.executable
    server = Proc("server", [py, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", APP_PORT, "--log-level", "warning"], APP)
    relay = Proc("relay", [py, "relay_publisher.py"], APP)
    v1 = Proc("v1_bot", [py, "agent_bot.py", "--bot", "V1"], BOT)
    s5 = Proc("s5_bot", [py, "agent_bot.py", "--bot", "S5"], BOT)
    s6 = Proc("s6_bot", [py, "agent_bot.py", "--bot", "S6"], BOT)
    s7 = Proc("s7_bot", [py, "agent_bot.py", "--bot", "S7"], BOT)
    bots = (v1, s5, s6, s7)
    procs = [server, v1, s5, s6, s7, relay]

    def on_signal(sig, _):
        STOP["why"] = f"signal {sig}"
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    STATUS["phase"] = "probing"
    probe()
    bad = [n for n, s in STATUS["sources"].items() if not s["ok"]]
    say("sources: " + ("all reachable" if not bad else "UNREACHABLE " + ", ".join(bad)))
    for p in procs:
        p.start()
        time.sleep(2)
    STATUS["phase"] = "running"
    say("bots running; handover at " + time.strftime("%H:%M", time.localtime(DEADLINE)))
    last_save, last_probe = time.time(), time.time()
    while not STOP["why"] and time.time() < DEADLINE:
        for p in procs:
            p.tick()
        if time.time() - last_probe >= 300:
            probe()
            last_probe = time.time()
        if time.time() - last_save >= SAVE_EVERY:
            save_state()
            last_save = time.time()
        write_status()
        time.sleep(15)

    STATUS["phase"] = "handover"
    write_status()
    say(f"handover ({STOP['why'] or 'time limit'}): stopping bots")
    for p in bots:
        p.restart = False
        p.stop(signal.SIGTERM, wait=120)   # bots persist open positions on SIGTERM and exit
    time.sleep(5)   # let the server/relay publish the 'handover' status once more
    for p in (relay, server):
        p.stop(signal.SIGTERM, wait=20)
    ok = save_state(final=True)
    if not STOP["why"] or os.environ.get("DISPATCH_ON_SIGNAL") == "1":
        dispatch_next()
    say("done" if ok else "done (final state save FAILED; next run resumes from the previous save)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
