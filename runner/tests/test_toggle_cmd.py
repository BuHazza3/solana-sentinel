"""Authenticated on/off command channel. Uses a throwaway test secret (never the real key). Run: python3 -m pytest -q tests/"""
import json, os, shutil, subprocess, sys, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # runner/
sys.path.insert(0, os.path.join(HERE, "app"))
import toggle_cmd as T

SECRET = "TEST-ONLY-not-the-real-key-0123456789"
BOTSRC = HERE


def mk(tmp_path):
    for f in ("v1_bot.toml", "s5_bot.toml"):
        shutil.copy(os.path.join(BOTSRC, f), tmp_path / f)
    topic, aes = T.derive(SECRET)
    return topic, aes, T.Handler(aes, str(tmp_path), str(tmp_path / "cmd.log"))


def cmd(bot="V1", agent="M4_SCAN", on=False, ts=None, n=None):
    return {"v": 1, "bot": bot, "agent": agent, "on": on, "ts": ts or time.time(), "n": n or os.urandom(8).hex()}


def state(tmp_path):
    return json.load(open(tmp_path / "data" / "agent_toggles.json"))


def test_apply_and_persist(tmp_path):
    _, aes, h = mk(tmp_path)
    assert h.handle(T.seal(aes, cmd())) == (True, "applied")
    d = state(tmp_path)
    assert d["bots"]["V1"]["M4_SCAN"] is False and d["rev"] == 1
    assert h.handle(T.seal(aes, cmd(agent="_master", on=False))) == (True, "applied")
    assert state(tmp_path)["bots"]["V1"]["_master"] is False and state(tmp_path)["rev"] == 2
    assert "APPLY V1/M4_SCAN -> OFF" in open(tmp_path / "cmd.log").read()


def test_rejects_wrong_key_and_garbage(tmp_path):
    _, aes, h = mk(tmp_path)
    _, other = T.derive("some-other-secret-abcdefghijklmnop")
    assert h.handle(T.seal(other, cmd())) == (False, "unauthenticated")
    assert h.handle(b"C1" + os.urandom(40)) == (False, "unauthenticated")
    assert h.handle(b"garbage") == (False, "unauthenticated")
    assert h.handle(None) == (False, "unauthenticated")
    blob = bytearray(T.seal(aes, cmd())); blob[-1] ^= 1
    assert h.handle(bytes(blob)) == (False, "unauthenticated")         # tampered
    # the STATE key (S1 frames) must not work for commands
    import hashlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    st = AESGCM(hashlib.sha256(("sentinel-key:" + SECRET).encode()).digest())
    assert h.handle(T.seal(st, cmd())) == (False, "unauthenticated")
    assert not (tmp_path / "data" / "agent_toggles.json").exists()


def test_rejects_unknown_stale_replay(tmp_path):
    _, aes, h = mk(tmp_path)
    assert h.handle(T.seal(aes, cmd(agent="TRADE_NOW"))) == (False, "unknown_switch")
    assert h.handle(T.seal(aes, cmd(bot="ORIGINAL"))) == (False, "unknown_switch")
    assert h.handle(T.seal(aes, cmd(on="yes"))) == (False, "unknown_switch")
    assert h.handle(T.seal(aes, {"v": 1, "bot": "V1"})) == (False, "malformed")
    assert h.handle(T.seal(aes, cmd(ts=time.time() - 600))) == (False, "stale")
    c = cmd(); b = T.seal(aes, c)
    assert h.handle(b) == (True, "applied")
    assert h.handle(b) == (False, "replay")                             # same frame again
    assert h.handle(T.seal(aes, dict(c, n="new-nonce"))) == (False, "replay")   # same ts, new nonce
    assert h.handle(T.seal(aes, cmd(ts=c["ts"] - 5, on=True))) == (False, "replay")  # older than the last applied
    assert h.handle(T.seal(aes, cmd(ts=c["ts"] + 1, on=True))) == (True, "applied")


def test_bot_reads_what_handler_wrote(tmp_path):
    _, aes, h = mk(tmp_path)
    sys.path.insert(0, BOTSRC)
    import agent_toggles
    h.handle(T.seal(aes, cmd(bot="S5", agent="REVIVAL", on=False)))
    w = agent_toggles.Watch("S5", {"REVIVAL": True, "COPY": True}, path=str(tmp_path / "data" / "agent_toggles.json"), every=0)
    assert not w.on("REVIVAL") and w.on("COPY")


def test_browser_frame_compatible(tmp_path):
    """Seal a command exactly like the Pages app does (WebCrypto in node) and check the bot side accepts it."""
    topic, aes, h = mk(tmp_path)
    js = r"""
const c = globalThis.crypto, enc = new TextEncoder(), hex = b => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('');
const sha = s => c.subtle.digest('SHA-256', enc.encode(s)); const KEY = process.argv[1];
(async () => { const t = 'sntl/' + hex(await sha('sentinel-cmd:' + KEY)).slice(0, 32);
  const k = await c.subtle.importKey('raw', await sha('sentinel-cmd-key:' + KEY), 'AES-GCM', false, ['encrypt']);
  const iv = c.getRandomValues(new Uint8Array(12)), n = hex(c.getRandomValues(new Uint8Array(12)));
  const ct = new Uint8Array(await c.subtle.encrypt({name: 'AES-GCM', iv, additionalData: enc.encode('sentinel-cmd')}, k,
    enc.encode(JSON.stringify({v: 1, bot: 'V1', agent: 'LS', on: false, ts: Date.now() / 1000, n}))));
  process.stdout.write(JSON.stringify({t, f: Buffer.from([67, 49, ...iv, ...ct]).toString('hex')})); })();
"""
    out = json.loads(subprocess.run(["node", "-e", js, SECRET], capture_output=True, text=True, check=True).stdout)
    assert out["t"] == topic
    assert h.handle(bytes.fromhex(out["f"])) == (True, "applied")
    assert state(tmp_path)["bots"]["V1"]["LS"] is False


def test_index_html_uses_same_derivation():
    for p in (os.path.join(os.path.dirname(HERE), "index.html"),):
        s = open(p).read()
        low = s.lower()
        for bad in ("window.solana", "window.phantom", "signtransaction", "sendtransaction", "/api/trade", "method: 'post'", 'method: "post"', "privatekey"):
            assert bad not in low, (p, bad)
        assert s.count("mpkt(0x30") <= 1          # the only publish = the switch command
    s = open(os.path.join(os.path.dirname(HERE), "index.html")).read()
    assert "'sentinel-cmd:' + KEY" in s and "'sentinel-cmd-key:' + KEY" in s and "additionalData: enc.encode('sentinel-cmd')" in s
    assert "mpkt(0x30, [...mstr(CTOPIC), 67, 49, ...iv, ...ct])" in s     # QoS0, not retained
