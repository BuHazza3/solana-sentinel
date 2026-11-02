"""On/off switches for the sub-agents (PAPER ONLY). The file data/agent_toggles.json is written only by the app side
(relay_publisher.py, after an authenticated command from the app); bots only READ it. Off = no new entries for that
sub-agent; its open positions are still managed to their exits. "_master" = the whole bot."""
import json, os, time

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_toggles.json")


def load(path=PATH):
    try:
        d = json.load(open(path))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def effective(bot, defaults, data):
    """defaults: {agent: bool} from the bot's toml. Returns {"_master": bool, agent: bool, ...}."""
    over = ((data or {}).get("bots") or {}).get(bot) or {}
    out = {"_master": bool(over.get("_master", True))}
    for a, on in defaults.items():
        out[a] = bool(over.get(a, on))
    return out


class Watch:
    """Re-reads the file when it changes (checked at most every `every` seconds)."""

    def __init__(self, bot, defaults, path=PATH, every=2.0):
        self.bot, self.defaults, self.path, self.every = bot, dict(defaults), path, every
        self.mtime, self.checked, self.data = None, 0.0, {}
        self.state = effective(bot, defaults, {})
        self.refresh(force=True)

    def refresh(self, force=False):
        now = time.time()
        if not force and now - self.checked < self.every:
            return self.state
        self.checked = now
        try:
            m = os.stat(self.path).st_mtime
        except OSError:
            m = None
        if force or m != self.mtime:
            self.mtime = m
            self.data = load(self.path) if m else {}
            self.state = effective(self.bot, self.defaults, self.data)
        return self.state

    def on(self, agent):
        s = self.refresh()
        return s["_master"] and s.get(agent, False)

    def rev(self):
        return (self.data or {}).get("rev", 0)
