"""S7 Copy Elite (PAPER ONLY): config, wallet ranking + tiers, tier sizing, exits, switches, command channel, state files."""
import io, json, os, shutil, sys, tarfile, time, tomllib
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "app"))
import s5_core as C
import agent_exits as EXI
import agent_toggles
import s7_rank as R

A = tomllib.load(open(os.path.join(HERE, "agents.toml"), "rb"))
B = tomllib.load(open(os.path.join(HERE, "s7_bot.toml"), "rb"))
CFG = {k: v for k, v in B["ranking"].items() if k in R.DEFAULT}


def res(*rets, t0=1000):
    return [{"ts": t0 + i, "ret": r} for i, r in enumerate(rets)]


# ---------------------------------------------------------------- config
def test_s7_config_paper_copy_only_and_band():
    assert B["general"]["paper"] is True and A["general"]["paper"] is True and B["general"]["start_balance_sol"] == 1.0
    assert set(B["agents"]) == {"S7_TOP", "S7_NEXT"} and all(B["agents"].values())          # both ON by default
    R_ = B["risk"]
    assert R_["max_open"] == 4 and R_["daily_stop_pct"] == 10.0 and R_["max_mcap_usd"] <= 1_000_000 and R_["sizing"] == "tier"
    for a in B["agents"]:
        S = A["agent"][a]
        assert S["kind"] == "v1" and S["signal"] == "copy" and S["exit_when_leader_sells"] is True   # copy-trade only, V1 rug filter
        assert S["min_mcap_usd"] == 5000 and S["max_mcap_usd"] <= 1_000_000 and S["min_liquidity_usd"] == 5000
        X = S["exit"]
        assert X["engine"] == "card" and X["stop_loss_pct"] == 32 and X["take_profits"] == [[2.0, 0.55], [5.0, 1.0]]
        assert X["trail_pct"] == 30 and X["max_hold_minutes"] == 120 and X["no_lift_minutes"] == 45 and X["no_lift_x"] == 1.25
    assert A["agent"]["S7_TOP"]["tier"] == 1 and A["agent"]["S7_NEXT"]["tier"] == 2


def test_s7_loss_per_stop_about_v1():
    """Wider stop, smaller size: a full stop costs about what V1's 15% x 20% stop cost (3% of cash)."""
    R_, stop = B["risk"], A["agent"]["S7_TOP"]["exit"]["stop_loss_pct"]
    assert abs(R_["tier1_pct"] * stop / 100 - 3.0) < 0.5 and R_["tier2_pct"] * stop / 100 < 3.0


# ---------------------------------------------------------------- ranking
def test_rank_min_sample_profit_and_tiers():
    seed = {"good": res(0.5, -0.3, 0.4, 0.8, -0.2), "ok": res(0.3, -0.1, 0.2), "few": res(2.0, 1.0),
            "loser": res(-0.3, -0.3, 0.1, -0.3), "flat": res(0.1, -0.1, 0.0)}
    rows = R.rank(list(seed) + ["new"], seed, {}, CFG)
    t = R.tiers(rows)
    assert t["good"] == 1                 # >=5 results, PF >= 1.5, net > 0
    assert t["ok"] == 2                   # only 3 results -> tier 2
    assert t["few"] == 0 and "only 2" in next(r for r in rows if r["wallet"] == "few")["why"]
    assert t["loser"] == 0 and t["flat"] == 0 and t["new"] == 0
    assert rows[0]["wallet"] == "good"    # best first


def test_rank_caps_tiers_and_copied_count():
    seed = {f"w{i}": res(*([0.5 + i / 10] * 6)) for i in range(8)}
    rows = R.rank(list(seed), seed, {}, CFG)
    t = R.tiers(rows)
    assert sum(1 for v in t.values() if v == 1) == CFG["top_n"] == 2
    assert sum(1 for v in t.values() if v) == CFG["max_copied"] == 5
    assert t["w7"] == 1 and t["w6"] == 1 and t["w0"] == 0     # ranked by net


def test_drop_after_three_own_losses_in_a_row():
    seed = {"good": res(0.5, 0.4, 0.8, 0.6, 0.3, 0.2)}
    assert R.tiers(R.rank(["good"], seed, {"good": res(-0.32, -0.32, t0=5000)}, CFG))["good"] == 1
    rows = R.rank(["good"], seed, {"good": res(-0.32, -0.32, -0.05, t0=5000)}, CFG)
    assert rows[0]["tier"] == 0 and rows[0]["dropped"] == "3 losses in a row" and rows[0]["why"].startswith("dropped")


def test_drop_when_last_five_own_negative():
    seed = {"w": res(*([0.9] * 10))}
    own = {"w": res(0.2, -0.32, 0.1, -0.32, 0.05, t0=9000)}   # no 3-loss streak, but last 5 sum < 0
    rows = R.rank(["w"], seed, own, CFG)
    assert rows[0]["tier"] == 0 and "last 5" in rows[0]["dropped"]


def test_rolling_window_uses_last_results():
    seed = {"w": res(*([-0.3] * 30 + [0.5] * 20))}            # old losses fall out of the 20-result window
    r = R.rank(["w"], seed, {}, CFG)[0]
    assert r["n"] == 20 and r["net"] > 0 and r["tier"] == 1


def test_own_trades_re_rank():
    seed = {"w": res(0.2, -0.1, 0.1)}
    assert R.tiers(R.rank(["w"], seed, {}, CFG))["w"] == 2
    own = {"w": res(0.9, 0.6, t0=9000)}                        # S7's own wins promote it (5 results, PF >= 1.5)
    assert R.tiers(R.rank(["w"], seed, own, CFG))["w"] == 1


def test_seed_file_ranking_excludes_slingoor():
    seed = json.load(open(os.path.join(HERE, B["ranking"]["seed_file"])))["wallets"]
    W = tomllib.load(open(os.path.join(HERE, "wallets.toml"), "rb"))["follow"]
    rows = R.rank([w["label"] for w in W], seed, {}, CFG)
    t = R.tiers(rows)
    assert t["slingoor"] == 0                                   # negative in the replay -> not copied
    assert all(r["net"] > 0 and r["n"] >= CFG["min_trades"] for r in rows if r["tier"])
    assert len(rows) == len(W)


# ---------------------------------------------------------------- sizing
def test_tier_sizing():
    Rk = B["risk"]
    assert abs(R.size_for_tier(1, 1.0, Rk) - 0.10) < 1e-12
    assert abs(R.size_for_tier(2, 1.0, Rk) - 0.06) < 1e-12
    assert abs(R.size_for_tier(1, 0.5, Rk) - 0.05) < 1e-12 and abs(R.size_for_tier(2, 0.5, Rk) - 0.03) < 1e-12
    assert R.size_for_tier(1, 5.0, Rk) == 0.10                  # capped
    assert R.size_for_tier(0, 1.0, Rk) == 0.0 and R.size_for_tier(None, 1.0, Rk) == 0.0
    assert R.size_for_tier(2, 0.06, Rk) == 0.0                  # keeps the reserve, below the minimum trade


# ---------------------------------------------------------------- exits (card engine)
def pos(a="S7_TOP"):
    return C.Position(1.0, 0, EXI.card_of(A["agent"][a]["exit"]), "graduated")


def test_stop_loss_32():
    p = pos()
    assert p.step(60, 1.0, 1.05, 0.75, 0.8, "graduated") == []   # -25% intrabar: still in (wider stop)
    acts = p.step(120, 0.8, 0.82, 0.6, 0.65, "graduated")
    assert p.closed and acts[0][2] == "stop_loss" and acts[0][1] == 0.65   # closed below the -32% stop: filled at the close (conservative)


def test_stake_out_at_2x_then_30pct_trail_then_5x():
    p = pos()
    acts = p.step(60, 1.0, 2.1, 0.99, 2.0, "graduated")
    assert acts[0][0] == 0.55 and acts[0][2] == "take_profit_2x" and not p.closed
    assert p.step(120, 2.0, 3.0, 1.9, 2.9, "graduated") == []    # peak 3.0 -> stop 2.1
    assert p.step(180, 2.9, 2.95, 2.2, 2.3, "graduated") == []
    acts = p.step(240, 2.3, 2.3, 2.0, 2.05, "graduated")
    assert p.closed and acts[0][2] == "trailing_stop" and abs(acts[0][0] - 0.45) < 1e-9
    q = pos()
    q.step(60, 1.0, 2.1, 0.99, 2.0, "graduated")
    acts = q.step(120, 2.0, 5.5, 2.0, 5.2, "graduated")
    assert q.closed and acts[-1][2] == "take_profit_5x"


def test_no_lift_exit_at_45_min_and_max_hold_120():
    p = pos()
    for m in range(1, 45):
        assert p.step(m * 60, 1.0, 1.2, 0.9, 1.0, "graduated") == []   # never reached 1.25x
    acts = p.step(45 * 60, 1.0, 1.1, 0.95, 1.0, "graduated")
    assert p.closed and acts[0][2] == "no_lift_time_exit"
    q = pos()
    q.step(60, 1.0, 1.3, 0.95, 1.2, "graduated")                     # reached 1.25x -> may run to 120 min
    for m in range(2, 120):
        assert q.step(m * 60, 1.1, 1.15, 1.0, 1.1, "graduated") == []
    acts = q.step(120 * 60, 1.1, 1.1, 1.0, 1.05, "graduated")
    assert q.closed and acts[0][2] == "time_exit"


def test_live_card_tick_and_no_lift_are_optional_for_old_cards():
    X = A["agent"]["S6_COPY"]["exit"]                                  # old cards have no no_lift keys -> unchanged
    c = EXI.card_of(X)
    assert c["stale_min"] is None
    p = C.Position(1.0, 0, c, "graduated")
    for m in range(1, 29):
        assert p.step(m * 60, 1.0, 1.1, 0.95, 1.0, "graduated") == []
    p7 = {"engine": pos().state(), "last_mark": 1.0, "peak_mark": 1.0, "last_ok": 0, "opened": 0}
    acts = EXI.card(p7, A["agent"]["S7_NEXT"]["exit"], 46 * 60, 1.0, "graduated")
    assert acts == [("final", "no_lift_time_exit")]


# ---------------------------------------------------------------- switches / channel / state
def test_s7_switch_defaults_and_overrides():
    eff = agent_toggles.effective("S7", B["agents"], {})
    assert eff["_master"] and eff["S7_TOP"] and eff["S7_NEXT"]
    eff = agent_toggles.effective("S7", B["agents"], {"bots": {"S7": {"S7_NEXT": False}, "S6": {"_master": False}}})
    assert eff["_master"] and eff["S7_TOP"] and not eff["S7_NEXT"]
    eff = agent_toggles.effective("S7", B["agents"], {"bots": {"S7": {"_master": False}}})
    assert not eff["_master"]


def test_command_channel_knows_s7(tmp_path):
    import toggle_cmd as T
    for f in ("v1_bot.toml", "s5_bot.toml", "s6_bot.toml", "s7_bot.toml"):
        shutil.copy(os.path.join(HERE, f), tmp_path / f)
    al = T.allowed_agents(str(tmp_path))
    assert al["S7"] == {"_master", "S7_TOP", "S7_NEXT"}
    topic, aes = T.derive("TEST-ONLY-not-the-real-key-0123456789")
    h = T.Handler(aes, str(tmp_path), str(tmp_path / "cmd.log"))
    cmd = {"v": 1, "bot": "S7", "agent": "S7_TOP", "on": False, "ts": time.time(), "n": os.urandom(8).hex()}
    assert h.handle(T.seal(aes, cmd)) == (True, "applied")
    assert json.load(open(tmp_path / "data" / "agent_toggles.json"))["bots"]["S7"]["S7_TOP"] is False
    bad = dict(cmd, agent="V1_SNIPE", n=os.urandom(8).hex(), ts=time.time() + 1)
    assert h.handle(T.seal(aes, bad))[0] is False


def test_statestore_saves_s7_files(tmp_path):
    import statestore
    bot, app = tmp_path / "bot", tmp_path / "app"
    (bot / "data").mkdir(parents=True); (app / "logs").mkdir(parents=True)
    (bot / "data" / "s7_bot_run.txt").write_text("S720261104-010000")
    (bot / "data" / "s7_bot_account.json").write_text(json.dumps({"run": "S720261104-010000", "wallet_own": {"Sheep": [{"ts": 1, "ret": 0.5}]}}))
    names = tarfile.open(fileobj=io.BytesIO(statestore.pack(str(bot), str(app)))).getnames()
    assert "bot/data/s7_bot_account.json" in names and "bot/data/s7_bot_run.txt" in names


def test_runner_starts_s7_and_app_has_tab():
    src = open(os.path.join(HERE, "runner.py")).read()
    assert '"--bot", "S7"' in src
    html = open(os.path.join(os.path.dirname(HERE), "index.html")).read()
    assert "['S7', 'S7 Copy']" in html and "wallet_rank" in html


def test_bots_view_rules_mention_s7():
    os.environ["BOT_DIR"] = HERE
    import importlib, bots_view
    importlib.reload(bots_view)
    keys = [r.get("key") for r in bots_view.rules()]
    assert "S7_TOP" in keys and "S7_NEXT" in keys
