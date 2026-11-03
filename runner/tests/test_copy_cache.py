import os, sys, tempfile, importlib, multiprocessing as mp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot import copytrade as CT


class FakeRPC:
    def __init__(self, sig="s1", fail=False):
        self.calls, self.sig, self.fail = [], sig, fail
        self.bal = {"M1pump": 5.0}

    def call(self, method, params):
        self.calls.append(method)
        if self.fail:
            return None
        if method == "getSignaturesForAddress":
            return [{"signature": self.sig}]
        info = [{"account": {"data": {"parsed": {"info": {"mint": m, "tokenAmount": {"uiAmount": v}}}}}} for m, v in self.bal.items()]
        return {"value": info if params[1]["programId"].startswith("Tokenkeg") else []}


def setup(tmp):
    CT.CACHE_DIR = tmp


def test_same_result_as_direct():
    with tempfile.TemporaryDirectory() as d:
        setup(d); r = FakeRPC()
        assert CT.shared_balances(r, "W1", {}) == CT.balances(FakeRPC(), "W1") == {"M1pump": 5.0}


def test_fresh_snapshot_reused_without_rpc():
    with tempfile.TemporaryDirectory() as d:
        setup(d); t = [1000.0]; st = {}
        CT.shared_balances(FakeRPC(), "W1", st, now=lambda: t[0])
        r2 = FakeRPC(); t[0] += 5
        assert CT.shared_balances(r2, "W1", st, now=lambda: t[0]) == {"M1pump": 5.0}
        assert r2.calls == [] and st["copy_cache_hit"] == 1


def test_idle_wallet_skips_heavy_call_and_new_tx_refetches():
    with tempfile.TemporaryDirectory() as d:
        setup(d); t = [1000.0]; st = {}
        CT.shared_balances(FakeRPC(), "W1", st, now=lambda: t[0])
        r = FakeRPC(); r.bal = {"M1pump": 9.0}; t[0] += 60
        assert CT.shared_balances(r, "W1", st, now=lambda: t[0]) == {"M1pump": 5.0}     # same signature -> cached
        assert r.calls == ["getSignaturesForAddress"] and st["copy_idle_skip"] == 1
        r = FakeRPC(sig="s2"); r.bal = {"M1pump": 9.0}; t[0] += 30
        assert CT.shared_balances(r, "W1", st, now=lambda: t[0]) == {"M1pump": 9.0}     # new tx -> full snapshot
        r = FakeRPC(sig="s2"); r.bal = {"M1pump": 7.0}; t[0] += CT.IDLE_MAX_S + 1
        assert CT.shared_balances(r, "W1", st, now=lambda: t[0]) == {"M1pump": 7.0}     # forced refresh after IDLE_MAX_S


def test_rpc_failure_returns_none():
    with tempfile.TemporaryDirectory() as d:
        setup(d)
        assert CT.shared_balances(FakeRPC(fail=True), "W1", {}) is None


def _worker(d, q):
    CT.CACHE_DIR = d; r = FakeRPC()
    q.put((CT.shared_balances(r, "W1", {}), r.calls.count("getTokenAccountsByOwner")))


def test_three_processes_one_fetch():
    with tempfile.TemporaryDirectory() as d:
        q = mp.Queue(); ps = [mp.Process(target=_worker, args=(d, q)) for _ in range(3)]
        [p.start() for p in ps]; [p.join(10) for p in ps]
        res = [q.get(timeout=5) for _ in ps]
        assert all(b == {"M1pump": 5.0} for b, _ in res)
        assert sum(n for _, n in res) == 2          # one snapshot (2 token programs) shared by all three
