"""S6 Combo (PAPER ONLY): config sanity, AK exit rule on the card engine, switches default ON, command channel knows S6."""
import os, sys, tomllib
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "app"))
import s5_core as C
import agent_exits as EXI
import agent_toggles

A = tomllib.load(open(os.path.join(HERE, "agents.toml"), "rb"))
B = tomllib.load(open(os.path.join(HERE, "s6_bot.toml"), "rb"))


def test_s6_config_paper_pump_and_cap():
    assert B["general"]["paper"] is True and A["general"]["paper"] is True
    assert B["general"]["start_balance_sol"] == 1.0
    assert B["risk"]["max_mcap_usd"] <= 1_000_000
    assert set(B["agents"]) == {"S6_COPY", "S6_CONFL", "S6_CONFIRM", "S6_CGRAD"}
    assert all(B["agents"].values())                      # defaults ON (AK asked for S6)
    for a in B["agents"]:
        S = A["agent"][a]
        assert S["max_mcap_usd"] <= 1_000_000
        X = S["exit"]
        assert X["engine"] == "card" and X["take_profits"][0] == [2.0, 0.55] and X["take_profits"][-1][1] == 1.0
        assert 0 < X["stop_loss_pct"] <= 25 and X["max_hold_minutes"] <= 60


def test_ak_rule_stake_out_at_2x_then_moonbag_trails_wider():
    X = A["agent"]["S6_COPY"]["exit"]
    p = C.Position(1.0, 0, EXI.card_of(X), "graduated")
    assert p.step(60, 1.0, 1.3, 0.98, 1.28, "graduated") == []          # armed protective trail at 1.25x
    acts = p.step(120, 1.28, 2.05, 1.27, 2.0, "graduated")
    assert acts and acts[0][0] == 0.55 and acts[0][2] == "take_profit_2x"
    assert p._trail() == X["trail_after_tp_pct"]                         # moonbag: wider trail
    assert p.step(180, 2.0, 2.6, 1.95, 2.5, "graduated") == []           # 2.6 peak; 30% trail = 1.82
    assert p.step(2400, 2.5, 2.55, 2.0, 2.2, "graduated") == []          # past 30 min but moonbag timeout is 120 min
    acts = p.step(300 * 60, 2.2, 5.2, 2.1, 5.0, "graduated")
    assert p.closed or acts[-1][2] in ("take_profit_5x", "moonbag_time_exit")


def test_protective_trail_before_2x():
    X = A["agent"]["S6_COPY"]["exit"]
    p = C.Position(1.0, 0, EXI.card_of(X), "graduated")
    p.step(60, 1.0, 1.5, 0.99, 1.45, "graduated")                       # peak 1.5 -> trail stop 1.32
    acts = p.step(120, 1.45, 1.46, 1.2, 1.25, "graduated")
    assert p.closed and acts[0][2] == "trailing_stop" and acts[0][1] == 1.25   # closed below the stop: filled at the close (conservative)


def test_stop_loss():
    X = A["agent"]["S6_CONFL"]["exit"]
    p = C.Position(1.0, 0, EXI.card_of(X), "bonding")
    acts = p.step(60, 0.95, 0.96, 0.8, 0.82, "bonding")
    assert p.closed and acts[0][2] == "stop_loss"


def test_s6_switch_defaults_and_overrides():
    eff = agent_toggles.effective("S6", B["agents"], {})
    assert eff["_master"] and all(eff[a] for a in B["agents"])
    eff = agent_toggles.effective("S6", B["agents"], {"bots": {"S6": {"S6_CGRAD": False}, "V1": {"_master": False}}})
    assert eff["_master"] and not eff["S6_CGRAD"] and eff["S6_COPY"]


def test_command_channel_allows_s6(tmp_path):
    import toggle_cmd
    for f in ("v1_bot.toml", "s5_bot.toml", "s6_bot.toml"):
        (tmp_path / f).write_bytes(open(os.path.join(HERE, f), "rb").read())
    allow = toggle_cmd.allowed_agents(str(tmp_path))
    assert allow["S6"] == set(B["agents"]) | {"_master"}
    assert "S6_COPY" not in allow["V1"] and "S6_COPY" not in allow["S5"]
