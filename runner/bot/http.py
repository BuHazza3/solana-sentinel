"""Tiny HTTP helper with per-host rate limiting, retries and source health stats."""
import time, threading, collections, logging
from urllib.parse import urlparse
import requests

log = logging.getLogger("http")
_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (paper-bot)", "Accept": "application/json"})
_lock = threading.Lock()
_last = collections.defaultdict(float)
MIN_INTERVAL = {  # seconds between calls per host (free-tier friendly)
    "api.dexscreener.com": 0.25,
    "api.rugcheck.xyz": 0.6,
    "lite-api.jup.ag": 1.1,
    "api.mainnet-beta.solana.com": 0.28,
}
STATS = collections.defaultdict(lambda: {"ok": 0, "fail": 0, "last_error": ""})


def _wait(host):
    with _lock:
        gap = MIN_INTERVAL.get(host, 0.2)
        now = time.time()
        delay = _last[host] + gap - now
        _last[host] = max(now, _last[host] + gap)
    if delay > 0:
        time.sleep(delay)


def request(method, url, retries=3, timeout=15, **kw):
    host = urlparse(url).netloc
    err = ""
    for attempt in range(retries):
        _wait(host)
        try:
            r = _session.request(method, url, timeout=timeout, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                err = f"HTTP {r.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
                break
            STATS[host]["ok"] += 1
            return r.json()
        except Exception as e:  # network / json errors
            err = repr(e)[:120]
            time.sleep(1.0 * (attempt + 1))
    STATS[host]["fail"] += 1
    STATS[host]["last_error"] = err
    log.debug("request failed %s %s", url, err)
    return None


def get(url, **kw):
    return request("GET", url, **kw)


def post(url, json=None, **kw):
    return request("POST", url, json=json, **kw)
