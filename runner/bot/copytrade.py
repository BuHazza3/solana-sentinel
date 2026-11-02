"""Copy-trade watcher (read-only). Detects followed wallets' buys/sells by diffing their SPL token
balances between polls (getTokenAccountsByOwner on public RPC). This is robust to the airdrop-spam
that floods famous wallets' transaction history. Dust/airdrops are ignored by a minimum SOL value."""
import time, threading, logging
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
                cur = balances(self.rpc, addr)
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
