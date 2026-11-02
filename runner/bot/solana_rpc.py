"""Public Solana RPC helpers + swap extraction from parsed transactions (read-only)."""
from . import http

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
IGNORE_MINTS = {WSOL} | STABLES


class RPC:
    def __init__(self, url):
        self.url = url

    def call(self, method, params):
        r = http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not r or "error" in r:
            return None
        return r.get("result")

    def signatures(self, address, limit=100, before=None, until=None):
        opts = {"limit": limit}
        if before: opts["before"] = before
        if until: opts["until"] = until
        return self.call("getSignaturesForAddress", [address, opts]) or []

    def tx(self, sig):
        return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])


def extract_swap(tx, wallet):
    """Return dict(mint, side, sol, tokens, ts, sig) if tx is a simple SOL<->token swap by wallet, else None.
    sol = SOL spent (buy) or received (sell), net of the fee paid by the wallet (it is in the balance delta)."""
    if not tx or not tx.get("meta") or tx["meta"].get("err"):
        return None
    meta, msg = tx["meta"], tx["transaction"]["message"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    if wallet not in keys:
        return None
    i = keys.index(wallet)
    k = msg["accountKeys"][i]
    if isinstance(k, dict) and not k.get("signer"):
        return None  # wallet only mentioned (airdrop spam/dust), not acting
    sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
    deltas = {}
    for side, arr in (("pre", meta.get("preTokenBalances") or []), ("post", meta.get("postTokenBalances") or [])):
        for b in arr:
            if b.get("owner") != wallet:
                continue
            amt = float(b["uiTokenAmount"].get("uiAmount") or 0)
            deltas[b["mint"]] = deltas.get(b["mint"], 0) + (amt if side == "post" else -amt)
    sol_delta += deltas.pop(WSOL, 0)  # wrapped SOL counts as SOL
    toks = {m: d for m, d in deltas.items() if abs(d) > 0 and m not in IGNORE_MINTS}
    if len(toks) != 1:
        return None
    mint, d = next(iter(toks.items()))
    if d > 0 and sol_delta < -0.001:
        side = "buy"
    elif d < 0 and sol_delta > 0.0005:
        side = "sell"
    else:
        return None
    return {"mint": mint, "side": side, "sol": abs(sol_delta), "tokens": abs(d),
            "ts": tx.get("blockTime"), "sig": tx["transaction"]["signatures"][0]}
