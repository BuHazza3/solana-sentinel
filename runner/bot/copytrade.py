"""Copy-trade watcher (read-only). Detects followed wallets' buys/sells by diffing their SPL token
balances between polls (getTokenAccountsByOwner on public RPC). This is robust to the airdrop-spam
that floods famous wallets' transaction history. Dust/airdrops are ignored by a minimum SOL value."""
import time, threading, logging, os, json, fcntl
from .solana_rpc import RPC, IGNORE_MINTS
from . import sources

log = logging.getLogger("copy")
PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]


def balances(rpc, wallet):
    out = {}
    for prog in PROGRAMS:
        r = rpc.call("getTokenAccountsByOwner", [wallet, {"programId": prog}, {"encoding": "jsonParsed"}])
        if r is None:
            return None
        for a in r.get("value", []):
            info = a["account"]["data"]["parsed"]["info"]
            m = info["mint"]
            if m in IGNORE_MINTS:
                continue
            out[m] = out.get(m, 0) + float(info["tokenAmount"].get("uiAmount") or 0)
    return out


# Shared snapshot cache. V1, S5 and S6 watch the same wallets from one GitHub runner IP. A full balance snapshot of
# the 12 wallets is ~38 MB of jsonParsed RPC data, so three independent pollers hit the public RPC's per-IP limits
# (79% of polls failed with 3 bots). One process fetches a wallet; the others reuse that snapshot. A cheap
# getSignaturesForAddress(limit=1) check skips the heavy call when the wallet has done nothing since the last snapshot.
CACHE_DIR = os.environ.get("COPY_CACHE_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "wallet_cache")
FRESH_S = 20          # another bot's snapshot younger than this is reused as-is
IDLE_MAX_S = 300      # with an unchanged latest signature, a snapshot is reused for at most this long


def _latest_sig(rpc, wallet):
    r = rpc.call("getSignaturesForAddress", [wallet, {"limit": 1}])
    if r is None:
        return None
    return r[0]["signature"] if r else ""


def shared_balances(rpc, wallet, stats=None, now=time.time):
    """balances() through a cross-process file cache (same result format, None on RPC failure)."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        f = os.path.join(CACHE_DIR, wallet + ".json")
        lk = open(f + ".lock", "a")
    except OSError:
        return balances(rpc, wallet)
    with lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            c = json.load(open(f))
        except Exception:
            c = None
        t = now()
        if c and t - c["ts"] < FRESH_S:
            if stats is not None: stats["copy_cache_hit"] = stats.get("copy_cache_hit", 0) + 1
            return c["bal"]
        sig = _latest_sig(rpc, wallet)
        if c and sig is not None and sig == c.get("sig") and t - c.get("full_ts", c["ts"]) < IDLE_MAX_S:
            c["ts"] = t
            if stats is not None: stats["copy_idle_skip"] = stats.get("copy_idle_skip", 0) + 1
        else:
            bal = balances(rpc, wallet)
            if bal is None:
                return None
            c = {"ts": t, "full_ts": t, "sig": sig, "bal": bal}
        tmp = f"{f}.{os.getpid()}.tmp"
        json.dump(c, open(tmp, "w"))
        os.replace(tmp, f)
        return c["bal"]


class CopyWatcher(threading.Thread):
    def __init__(self, cfg, signal_cb, stats):
        super().__init__(daemon=True)
        self.cfg, self.cb, self.stats = cfg, signal_cb, stats
        self.rpc = RPC(cfg["rpc"]["url"])
        self.wallets = cfg["copytrade"]["wallets"]
        self.prev = {}
        self.stop = threading.Event()

    def run(self):
        c = self.cfg["copytrade"]
        while not self.stop.is_set():
            t0 = time.time()
            for w in self.wallets:
                addr, label = w["address"], w.get("label", w["address"][:6])
                cur = shared_balances(self.rpc, addr, self.stats)
                if cur is None:
                    self.stats["copy_poll_fail"] += 1
                    continue
                self.stats["copy_polls"] += 1
                old = self.prev.get(addr)
                self.prev[addr] = cur
                if old is None:
                    continue  # first snapshot = baseline, never replay history
                inc = {m: cur[m] - old.get(m, 0) for m in cur if cur[m] - old.get(m, 0) > 0}
                dec = {m: old[m] - cur.get(m, 0) for m in old if old[m] - cur.get(m, 0) > 0}
                if not inc and not dec:
                    continue
                prices = sources.jup_prices(list(inc) + list(dec))
                sol_usd = (prices.get(sources.WSOL) or {}).get("usdPrice")
                for m, d in inc.items():
                    p = (prices.get(m) or {}).get("usdPrice")
                    if not p or not sol_usd:
                        continue  # no market price -> airdrop/spam dust
                    val_sol = d * p / sol_usd
                    if val_sol >= c["min_followed_buy_sol"]:
                        self.stats["copy_buys_seen"] += 1
                        log.info("COPY signal: %s bought %s (~%.2f SOL)", label, m, val_sol)
                        self.cb({"type": "copy_buy", "mint": m, "wallet": addr, "label": label, "value_sol": val_sol})
                for m, d in dec.items():
                    p = (prices.get(m) or {}).get("usdPrice")
                    if p and sol_usd and d * p / sol_usd >= c["min_followed_buy_sol"] / 2:
                        self.stats["copy_sells_seen"] += 1
                        self.cb({"type": "copy_sell", "mint": m, "wallet": addr, "label": label})
            self.stop.wait(max(1, c["poll_seconds"] - (time.time() - t0)))
