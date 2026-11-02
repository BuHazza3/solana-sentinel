"""pump.fun public frontend API (no key) + pump-specific launch analysis. Read-only.
Endpoints verified working on 2026-11-02: /coins (list, sort/complete/creator filters), /trades/<chain>/<mint>, /sol-price.
Not available: single-coin lookup (/coins/<mint> 404), candlesticks (404), swap-api trades (410 gone)."""
import time
from . import http

PF = "https://frontend-api-v3.pump.fun"
CHAIN = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SUPPLY = 1_000_000_000
CURVE_RESERVED = 206_900_000      # tokens left in the curve at graduation (standard pump.fun tokenomics)
CURVE_SELLABLE = 793_100_000
http.MIN_INTERVAL["frontend-api-v3.pump.fun"] = 0.35


def discover():
    """{mint: (tag, coin_dict)} from several pump.fun lists."""
    out = {}
    lists = [("pf_new", "sort=created_timestamp&order=DESC"),
             ("pf_active", "sort=last_trade_timestamp&order=DESC"),
             ("pf_near_grad", "sort=market_cap&order=DESC&complete=false"),
             ("pf_grad_active", "sort=last_trade_timestamp&order=DESC&complete=true")]
    for tag, q in lists:
        d = http.get(f"{PF}/coins?offset=0&limit=50&{q}&includeNsfw=false") or []
        for c in d if isinstance(d, list) else []:
            if c.get("mint"):
                out.setdefault(c["mint"], (tag, c))
    d = http.get(f"{PF}/coins/currently-live?offset=0&limit=50&includeNsfw=false") or []
    for c in d if isinstance(d, list) else []:
        if c.get("mint"):
            out.setdefault(c["mint"], ("pf_live", c))
    return out


_creator_cache = {}


def creator_history(creator):
    """Dev's past launches: count, graduated count, best ATH market cap (USD)."""
    if not creator:
        return None
    c = _creator_cache.get(creator)
    if c and time.time() - c[0] < 1800:
        return c[1]
    d = http.get(f"{PF}/coins?offset=0&limit=50&creator={creator}&includeNsfw=true")
    if not isinstance(d, list):
        _creator_cache[creator] = (time.time(), None)
        return None
    res = {"coins": len(d), "graduated": sum(1 for x in d if x.get("complete")),
           "max_ath_mcap_usd": max([x.get("ath_market_cap") or 0 for x in d] or [0]), "capped_at_50": len(d) >= 50}
    _creator_cache[creator] = (time.time(), res)
    return res


def trades(mint, max_pages=3):
    """Newest-first trades; returns (list, reached_launch: bool)."""
    allt, cursor = [], None
    for _ in range(max_pages):
        url = f"{PF}/trades/{CHAIN}/{mint}?limit=100" + (f"&cursor={cursor}" if cursor else "")
        d = http.get(url)
        if not d or "trades" not in d:
            return allt, False
        allt += d["trades"]
        cursor = d.get("cursor")
        if not cursor:
            return allt, True
    return allt, False


def launch_analysis(mint, creator):
    """Dev buy/sell and same-block bundle buys at launch, from pump.fun trade history."""
    tr, complete = trades(mint)
    res = {"trades_fetched": len(tr), "reached_launch": complete}
    if not tr:
        return res
    def amt(t):
        b = t.get("baseAmount") or {}
        return int(b.get("raw") or 0) / 10 ** int(b.get("decimals") or 6)
    dev_buy = sum(amt(t) for t in tr if t["trader"]["address"] == creator and t["side"] == "buy")
    dev_sell = sum(amt(t) for t in tr if t["trader"]["address"] == creator and t["side"] == "sell")
    res["dev_bought_pct_supply"] = round(dev_buy / SUPPLY * 100, 2)
    res["dev_sold_pct_of_buys"] = round(dev_sell / dev_buy * 100, 1) if dev_buy else (100.0 if dev_sell else 0.0)
    res["dev_sells"] = sum(1 for t in tr if t["trader"]["address"] == creator and t["side"] == "sell")
    if complete:
        first_block = int(tr[-1]["blockId"])
        early = [t for t in tr if int(t["blockId"]) <= first_block + 1 and t["side"] == "buy"]
        res["launch_block_buyers"] = len({t["trader"]["address"] for t in early if t["trader"]["address"] != creator})
        res["bundle_pct_supply"] = round(sum(amt(t) for t in early if t["trader"]["address"] != creator) / SUPPLY * 100, 2)
    recent = tr[:100]
    res["recent_unique_traders"] = len({t["trader"]["address"] for t in recent})
    return res


def curve_progress_from_report(report):
    """Bonding-curve progress % from RugCheck's pump_fun market (curve token balance)."""
    for m in (report or {}).get("markets") or []:
        if m.get("marketType") == "pump_fun":
            acc = m.get("liquidityAAccount") or {}
            bal = int(acc.get("amount") or 0) / 10 ** int(acc.get("decimals") or 6) if acc.get("amount") else None
            if bal is None:
                continue
            prog = (CURVE_SELLABLE - max(0, bal - CURVE_RESERVED)) / CURVE_SELLABLE * 100
            return round(min(100, max(0, prog)), 1)
    return None


def phase_of(report, pair, coin):
    """'bonding' or 'graduated'."""
    mt = {m.get("marketType") for m in (report or {}).get("markets") or []}
    if coin and coin.get("complete"):
        return "graduated"
    if pair and pair.get("dexId") not in (None, "pumpfun"):
        return "graduated"
    if mt - {"pump_fun"}:   # any AMM market (PumpSwap/Raydium/Meteora...) => migrated; the old curve market can persist
        return "graduated"
    if "pump_fun" in mt or (pair and pair.get("dexId") == "pumpfun"):
        return "bonding"
    return "graduated" if mt else "unknown"


def is_pump(mint, report=None, pair=None, coin=None):
    """Return the evidence string if the token is a pump.fun launch, else None."""
    if coin is not None:
        return "pump_api"
    lp = str((report or {}).get("launchpad") or "") + str((report or {}).get("deployPlatform") or "")
    if "pump" in lp.lower():
        return "rugcheck_launchpad"
    if pair and pair.get("dexId") in ("pumpfun", "pumpswap"):
        return "dexscreener_dexId"
    if any(m.get("marketType", "").startswith("pump_fun") for m in (report or {}).get("markets") or []):
        return "rugcheck_market"
    if mint.endswith("pump"):
        return "mint_suffix_only"
    return None


def pool_fee_pct(phase, mcap_sol):
    """pump.fun pool fee (pump.fun/docs/fees, updated 17 Oct 2026). Already inside Jupiter quote amounts;
    reported separately for transparency. Excludes the 0.45% web/app fee (not charged on API/aggregator trades)."""
    if phase == "bonding" or mcap_sol is None or mcap_sol < 420:
        return 1.25
    tiers = [(1470, 1.20), (2460, 1.15), (3440, 1.10), (4420, 1.05), (9820, 1.00), (14740, 0.95), (19650, 0.90),
             (24560, 0.85), (29470, 0.80), (34380, 0.75), (39300, 0.70), (44210, 0.65), (49120, 0.60)]
    for cap, fee in tiers:
        if mcap_sol < cap:
            return fee
    return 0.55
