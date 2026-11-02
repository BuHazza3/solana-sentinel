import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent_toggles as T


def test_defaults_and_overrides(tmp_path):
    f = tmp_path / "t.json"
    w = T.Watch("V1", {"A": True, "B": False}, path=str(f), every=0)
    assert w.on("A") and not w.on("B") and w.rev() == 0
    f.write_text(json.dumps({"rev": 3, "bots": {"V1": {"A": False, "B": True}, "S5": {"_master": False}}}))
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert not w.on("A") and w.on("B") and w.rev() == 3
    f.write_text(json.dumps({"rev": 4, "bots": {"V1": {"_master": False, "B": True}}}))
    os.utime(f, (time.time() + 10, time.time() + 10))
    assert not w.on("B")                               # master off wins
    assert w.refresh()["B"] is True                    # but the agent's own switch is kept


def test_garbage_file_means_defaults(tmp_path):
    f = tmp_path / "t.json"; f.write_text("not json")
    w = T.Watch("S5", {"R": True}, path=str(f), every=0)
    assert w.on("R") and w.refresh()["_master"] is True
