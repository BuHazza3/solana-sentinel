import json, os, shutil, sqlite3, sys, tempfile
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import paper_reset as PR

CFG = """[[reset]]
id = "v1-reset-test"
bot = "V1"
balance_sol = 1.0
label = "V1 before reset 3 Nov"
"""


def make(tmp, positions=None):
    data = os.path.join(tmp, "data"); os.makedirs(data)
    for f in ("v1_bot.toml", "s5_bot.toml"):
        shutil.copy(os.path.join(HERE, f), tmp)
    open(os.path.join(data, "v1_bot_run.txt"), "w").write("V1OLD")
    json.dump({"balance": 0.3275, "start": 1.0, "positions": positions or {}, "traded": ["M1pump"], "closed_n": 3, "pnl": -0.6725, "day": 20760,
               "day_eq": 0.547, "cooldown": {}, "agent_pnl": {"COPY": 0.1}, "agent_n": {"COPY": 2}, "run": "V1OLD"}, open(os.path.join(data, "v1_bot_account.json"), "w"))
    s5 = {"balance": 0.877, "start": 1.0, "run": "S5RUN", "day": 20760, "day_eq": 0.908}
    json.dump(s5, open(os.path.join(data, "s5_bot_account.json"), "w"))
    json.dump({"rows": [{"bot": "Original", "agent": "x", "wins": 1, "losses": 0, "net": 0.1, "decision": "kept"}], "by_key": {}, "note": "n"},
              open(os.path.join(data, "legacy_agents.json"), "w"))
    c = sqlite3.connect(os.path.join(data, "v1_bot.db"))
    c.execute("CREATE TABLE trades (run TEXT, bot TEXT, agent TEXT, phase TEXT, mint TEXT, symbol TEXT, opened REAL, closed REAL, size_sol REAL, sol_out REAL, fees_sol REAL, pnl_sol REAL, pnl_pct REAL, exit_reason TEXT, record TEXT)")
    for a, p in (("COPY", 0.05), ("COPY", 0.05), ("V1_SNIPE", -0.2)):
        c.execute("INSERT INTO trades (run, agent, pnl_sol) VALUES ('V1OLD', ?, ?)", (a, p))
    c.execute("INSERT INTO trades (run, agent, pnl_sol) VALUES ('V1OLDER', 'COPY', 9)")
    c.commit(); c.close()
    cfg = os.path.join(tmp, "paper_resets.toml"); open(cfg, "w").write(CFG)
    return data, cfg, s5


def test_reset_applies_once_and_archives():
    with tempfile.TemporaryDirectory() as tmp:
        data, cfg, s5 = make(tmp)
        assert PR.apply_resets(tmp, cfg, log=lambda m: None, now=1793714000) == ["v1-reset-test"]
        a = json.load(open(os.path.join(data, "v1_bot_account.json")))
        run = open(os.path.join(data, "v1_bot_run.txt")).read().strip()
        assert a["balance"] == 1.0 and a["start"] == 1.0 and a["pnl"] == 0 and a["closed_n"] == 0
        assert a["day"] is None and a["day_eq"] is None and a["run"] == run and run.startswith("V1") and run != "V1OLD"
        assert a["traded"] == ["M1pump"]
        L = json.load(open(os.path.join(data, "legacy_agents.json")))
        new = [r for r in L["rows"] if r["bot"] == "V1 before reset 3 Nov"]
        assert {(r["agent"], r["wins"], r["losses"], round(r["net"], 4)) for r in new} == {("COPY", 2, 0, 0.1), ("V1_SNIPE", 0, 1, -0.2)}
        assert L["rows"][0]["bot"] == "Original"                       # old archive rows kept
        assert json.load(open(os.path.join(data, "s5_bot_account.json"))) == s5   # S5 untouched
        # second startup (next handover): nothing changes
        snap = open(os.path.join(data, "v1_bot_account.json")).read()
        assert PR.apply_resets(tmp, cfg, log=lambda m: None, now=1793720000) == []
        assert open(os.path.join(data, "v1_bot_account.json")).read() == snap
        assert open(os.path.join(data, "v1_bot_run.txt")).read().strip() == run
        assert len(json.load(open(os.path.join(data, "legacy_agents.json")))["rows"]) == len(L["rows"])
        assert "v1-reset-test" in PR.applied_ids(data)


def test_open_positions_kept_and_funded():
    with tempfile.TemporaryDirectory() as tmp:
        pos = {"M2pump": {"agent": "COPY", "size_sol": 0.05, "tokens": 10}}
        data, cfg, _ = make(tmp, pos)
        PR.apply_resets(tmp, cfg, log=lambda m: None)
        a = json.load(open(os.path.join(data, "v1_bot_account.json")))
        assert a["positions"] == pos and abs(a["balance"] - 0.95) < 1e-9 and a["start"] == 1.0


def test_statestore_carries_applied_ids():
    import statestore
    assert "paper_resets_applied.json" in open(statestore.__file__).read()
