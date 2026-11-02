"""Authenticated on/off commands for the bots' sub-agents (PAPER ONLY; switches can only stop or allow NEW paper entries).
The app (GitHub Pages, holder of the private key) publishes  b"C1" + iv(12) + AES-256-GCM(json)  on a topic derived from the
same relay secret, with a SEPARATE derived key (sha256("sentinel-cmd-key:"+secret)) and AAD "sentinel-cmd". GCM authenticates
it, so only key holders can produce a command; a timestamp window (+-120 s), a per-switch monotonic timestamp and a nonce
cache stop replays. Valid commands are applied to <bot>/data/agent_toggles.json (atomic write), which the bots poll every 2 s.
Every command (applied or rejected) is logged. No trade, wallet or deposit actions exist in this channel."""
import hashlib, json, os, time, tomllib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

H = lambda s: hashlib.sha256(s.encode()).digest()
AAD = b"sentinel-cmd"
MAX_SKEW = 120


def derive(secret):
    return "sntl/" + H("sentinel-cmd:" + secret).hex()[:32], AESGCM(H("sentinel-cmd-key:" + secret))


def seal(aes, obj, iv=None):   # used by tests / tools; the app does the same in the browser
    iv = iv or os.urandom(12)
    return b"C1" + iv + aes.encrypt(iv, json.dumps(obj, separators=(",", ":")).encode(), AAD)


def allowed_agents(bot_dir):
    out = {}
    for b in ("V1", "S5"):
        try:
            out[b] = set(tomllib.load(open(os.path.join(bot_dir, f"{b.lower()}_bot.toml"), "rb"))["agents"]) | {"_master"}
        except (OSError, ValueError, KeyError):
            pass
    return out


class Handler:
    def __init__(self, aes, bot_dir, log_path):
        self.aes, self.bot_dir, self.log_path = aes, bot_dir, log_path
        self.path = os.path.join(bot_dir, "data", "agent_toggles.json")
        self.nonces = {}

    def _log(self, msg):
        try:
            with open(self.log_path, "a") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except OSError:
            pass

    def load(self):
        try:
            d = json.load(open(self.path))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def handle(self, blob, now=None):
        """Returns (ok, reason). Never raises on bad input."""
        now = now or time.time()
        try:
            if not isinstance(blob, (bytes, bytearray)) or len(blob) < 2 + 12 + 16 or blob[:2] != b"C1" or len(blob) > 4096:
                raise ValueError("bad frame")
            cmd = json.loads(self.aes.decrypt(bytes(blob[2:14]), bytes(blob[14:]), AAD))
        except Exception:
            self._log("REJECT unauthenticated/garbled command")
            return False, "unauthenticated"
        try:
            bot, agent, on, ts, n = cmd["bot"], cmd["agent"], cmd["on"], float(cmd["ts"]), str(cmd["n"])[:64]
        except (KeyError, TypeError, ValueError):
            self._log("REJECT malformed command"); return False, "malformed"
        allow = allowed_agents(self.bot_dir)
        if cmd.get("v") != 1 or bot not in allow or agent not in allow[bot] or not isinstance(on, bool):
            self._log(f"REJECT unknown switch {bot!r}/{agent!r}"); return False, "unknown_switch"
        if abs(now - ts) > MAX_SKEW:
            self._log(f"REJECT stale command {bot}/{agent} skew={now - ts:.0f}s"); return False, "stale"
        for k in [k for k, t in self.nonces.items() if now - t > 2 * MAX_SKEW]:
            self.nonces.pop(k, None)
        if n in self.nonces:
            self._log(f"REJECT replayed nonce {bot}/{agent}"); return False, "replay"
        d = self.load()
        key = f"{bot}/{agent}"
        if ts <= (d.get("last_ts") or {}).get(key, 0):
            self._log(f"REJECT out-of-order/replayed command {key}"); return False, "replay"
        self.nonces[n] = now
        d.setdefault("v", 1)
        d.setdefault("bots", {}).setdefault(bot, {})[agent] = on
        d.setdefault("last_ts", {})[key] = ts
        d["rev"] = int(d.get("rev", 0)) + 1
        d["updated"] = now
        d["log"] = (d.get("log") or [])[-49:] + [{"ts": now, "bot": bot, "agent": agent, "on": on}]
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        json.dump(d, open(tmp, "w"), indent=1)
        os.replace(tmp, self.path)
        self._log(f"APPLY {key} -> {'ON' if on else 'OFF'} (rev {d['rev']})")
        return True, "applied"
