"""Free public data sources: DexScreener, RugCheck, Jupiter (no keys needed)."""
import time
from . import http

DS = "https://api.dexscreener.com"
RC = "https://api.rugcheck.xyz/v1"
JUP = "https://lite-api.jup.ag"
WSOL = "So11111111111111111111111111111111111111112"


# ---------------- DexScreener ----------------
def ds_discover():
    """Return {mint: source_tag} of recently listed / boosted / trending Solana tokens."""
    found = {}
    for path, tag in (("/token-profiles/latest/v1", "ds_new_profile"),
                      ("/token-boosts/latest/v1", "ds_boost_latest"),
                      ("/token-boosts/top/v1", "ds_boost_top"),
                      ("/community-takeovers/latest/v1", "ds_cto")):
        data = http.get(DS + path) or []
        for x in data if isinstance(data, list) else []:
            if x.get("chainId") == "solana" and x.get("tokenAddress"):
                found.setdefault(x["tokenAddress"], tag)
    return found


def ds_pairs(mints):
    """Batch fetch pairs for up to 30 mints per call. Returns {mint: best_pair_dict_with_extras}."""
    out = {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = http.get(f"{DS}/tokens/v1/solana/{','.join(chunk)}") or []
        by_mint = {}
        for p in data if isinstance(data, list) else []:
            m = p.get("baseToken", {}).get("address")
            if m in chunk:
                by_mint.setdefault(m, []).append(p)
        for m, ps in by_mint.items():
            best = max(ps, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            created = [p.get("pairCreatedAt") for p in ps if p.get("pairCreatedAt")]
            best = dict(best)
            best["_first_created_ms"] = min(created) if created else None
            best["_total_liq"] = sum((p.get("liquidity") or {}).get("usd") or 0 for p in ps)
            out[m] = best
    return out


# ---------------- RugCheck ----------------
_rc_cache = {}


def rc_discover():
    found = {}
    for path, tag in (("/stats/new_tokens", "rc_new"), ("/stats/recent", "rc_recent"),
                      ("/stats/trending", "rc_trending"), ("/stats/verified", "rc_verified")):
        data = http.get(RC + path) or []
        for x in data if isinstance(data, list) else []:
            m = x.get("mint")
            if m:
                found.setdefault(m, tag)
    return found


def rc_report(mint, ttl=600):
    c = _rc_cache.get(mint)
    if c and time.time() - c[0] < ttl:
        return c[1]
    r = http.get(f"{RC}/tokens/{mint}/report", timeout=25)
    _rc_cache[mint] = (time.time(), r)
    return r


# ---------------- Jupiter ----------------
def jup_prices(mints):
    """{mint: {'usdPrice':..,'decimals':..}} via Jupiter Price API v3 (lite, free)."""
    out = {}
    mints = list(set(mints) | {WSOL})
    for i in range(0, len(mints), 50):
        d = http.get(f"{JUP}/price/v3?ids={','.join(mints[i:i+50])}") or {}
        for k, v in d.items():
            if isinstance(v, dict) and v.get("usdPrice"):
                out[k] = v
    return out


def jup_discover():
    """Jupiter Tokens API v2 (free lite tier): brand-new tokens + 5m/1h trending + organic score."""
    found = {}
    for path, tag in (("/tokens/v2/recent", "jup_recent"), ("/tokens/v2/toptrending/5m?limit=100", "jup_trend5m"),
                      ("/tokens/v2/toptrending/1h?limit=100", "jup_trend1h"), ("/tokens/v2/toporganicscore/5m?limit=100", "jup_organic5m")):
        data = http.get(JUP + path) or []
        for x in data if isinstance(data, list) else []:
            if x.get("id") and x["id"] != WSOL and x.get("id") not in STABLE_IDS:
                found.setdefault(x["id"], tag)
    return found


STABLE_IDS = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def jup_quote(input_mint, output_mint, amount_raw, slippage_bps=300):
    if amount_raw <= 0:
        return None
    return http.get(f"{JUP}/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}"
                    f"&amount={int(amount_raw)}&slippageBps={slippage_bps}&restrictIntermediateTokens=true")
