"""The gold set: clips counted by hand in full, fixed store splits, blind second counts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count import gold
from crossing_count.webapp import Setup
from crossing_count.wizard import Wizard
from crossing_count.wizard_app import create_wizard_app


@pytest.fixture
def counted(two_tile_video: dict[str, Any], tmp_path: Path) -> Wizard:
    w = Wizard(two_tile_video["video"], two_tile_video["dir"], tmp_path)
    w.set_mode("manual")
    w.set_marks(True)
    s = Setup(w.video, two_tile_video["dir"])
    w.set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                        [{"picture": 0, "name": "CN-9-PB1", "include": True},
                         {"picture": 1, "name": "CN-9-R2", "include": False}])
    w.set_direction("both")
    w.state["clock_start"] = "2026-09-12T11:30:00"  # from RetailNext's file name, normally
    w.set_store(code="CN-9", operator="Alex")
    return w


def _count(w: Wizard, marks: list[tuple[float, str]]) -> None:
    dur = float(w.state["duration_s"])
    for frac, d in marks:
        w.manual_add("CN-9-PB1", round(dur * frac, 2), d)
    w.manual_watched("CN-9-PB1", 0.0, dur)
    w.manual_done()


def test_only_a_count_of_every_moment_can_be_kept(counted: Wizard, tmp_path: Path) -> None:
    assert "Finish counting first." in gold.problems(counted.state)
    with pytest.raises(gold.GoldError, match="watched"):
        gold.save(counted.state, counted.run_dir, [], "", tmp_path)
    _count(counted, [(0.2, "in"), (0.6, "out")])
    assert gold.problems(counted.state) == []
    names = [k["name"] for k in counted.counts()["checks"] if k["ok"]]
    assert "Footage of CN-9-PB1 watched" in names and "Counting finished" in names
    clip = gold.save(counted.state, counted.run_dir, ["busy"], "a note", tmp_path)
    assert (clip["split"], clip["crossings"], clip["tags"]) == ("test", 2, ["busy"])
    rec = json.loads((gold.folder(tmp_path) / f"{clip['id']}.json").read_text())
    first = rec["reviews"][0]["crossings"][0]
    assert (first["camera"], first["direction"]) == ("CN-9-PB1", "in")
    assert first["clock"].startswith("11:30:") and rec["direction_convention"].startswith("In =")
    with pytest.raises(gold.GoldError, match="Unknown tag"):
        gold.save(counted.state, counted.run_dir, ["sunny"], "", tmp_path)
    # stores go to the sets in turn, and never move
    assert gold.split_for("OTHER-1", tmp_path) == "validation"
    assert gold.split_for("cn-9", tmp_path) == "test"


def test_a_second_person_counts_blind_and_disputes_stay_out(counted: Wizard, tmp_path: Path) -> None:
    _count(counted, [(0.2, "in"), (0.6, "out")])
    gold.save(counted.state, counted.run_dir, [], "", tmp_path)
    counted.recount()
    assert counted.state["manual"]["counts"] == [] and counted.state["store"]["operator"] == ""
    assert list((counted.dir / "history").glob("*second count*.json"))
    counted.set_store(operator="Sam")
    dur = float(counted.state["duration_s"])
    counted.manual_add("CN-9-PB1", round(dur * 0.2, 2) + 0.3, "in")  # Sam saw only the first
    counted.manual_watched("CN-9-PB1", 0.0, dur)
    counted.manual_done()
    clip = gold.save(counted.state, counted.run_dir, [], "", tmp_path)
    a = clip["agreement"]
    assert a["reviewers"] == ["Alex", "Sam"] and (a["agreed"], a["only_first"]) == (1, 1)
    assert a["status"] == "disagreement (unresolved)" and "only Alex" in a["where"][0]
    assert (clip["crossings"], clip["uncertain"]) == (1, 1)


def test_the_tools_crossings_go_onto_the_clips_clock() -> None:
    items = [{"kind": "counted", "t": 101.0, "direction": "in"},
             {"kind": "counted", "t": 150.0, "direction": "out"},  # after the clip
             {"kind": "counted", "t": 50.0, "direction": "in"},  # before it
             {"kind": "possible", "t": 120.2, "direction": "out"}]
    s = gold.score_camera([(1.2, "in"), (20.0, "out")], [], items, [{"start": 110.0, "end": 130.0}],
                          100.0, 30.0, ["in", "out"])
    d = s["by_direction"]
    assert (d["in"]["found"], d["in"]["pred"], d["out"]["missed"], d["out"]["pred"]) == (1, 1, 1, 0)
    assert (s["misses_on_list"], s["questions"], s["watch_s"]) == (1, 2, 20.0)


def test_an_automatic_count_covering_the_clip_is_found(tmp_path: Path) -> None:
    rec = {"period": {"start": "2026-09-12T11:30:00", "end": "2026-09-12T11:45:00"},
           "cameras": [{"sensor": "CN-9-PB1"}]}
    cam = tmp_path / "runs" / "clean-1125" / "cn-9-pb1"
    cam.mkdir(parents=True)
    (cam / "activity.json").write_text(json.dumps({
        "config": {"sensor": "CN-9-PB1"},
        "timebase": {"filename_interval": {"start": "2026-09-12T11:25:00",
                                           "end": "2026-09-12T11:45:00"}}}))
    assert gold.find_run(rec, tmp_path / "runs") is None  # no recorded detections
    (cam / "detections.pkl").write_bytes(b"")
    found = gold.find_run(rec, tmp_path / "runs")
    assert found is not None
    run_dir, offset, have = found
    assert (run_dir.name, offset, have) == ("clean-1125", 300.0, {"CN-9-PB1"})


def test_scoring_needs_clips_and_keeps_a_record(counted: Wizard, tmp_path: Path) -> None:
    with pytest.raises(gold.GoldError, match="No gold clip"):
        gold.evaluate("development", tmp_path)
    _count(counted, [(0.2, "in")])
    gold.save(counted.state, counted.run_dir, [], "", tmp_path)  # CN-9: the test set
    exp = gold.evaluate("test", tmp_path)
    (clip,) = exp["clips"]
    assert not clip["scored"] and "No automatic count" in clip["reason"]
    assert any(x.startswith("Insufficient sample") for x in exp["limits"])
    assert (gold.experiments_dir(tmp_path) / f"{exp['id']}.json").is_file()
    client = TestClient(create_wizard_app(tmp_path / "sites", tmp_path, folders=[tmp_path]))
    seen = client.get("/api/gold").json()
    assert seen["test_scorings"] == 1 and seen["sets"]["test"]["clips"] == 1
    assert client.get("/gold/").status_code == 200
    assert client.post("/api/gold/score", json={"which": "everything"}).status_code == 400
