"""The gold set: full counts by hand, clean and marked footage apart, fixed store sets, frozen
versions, and how far RetailNext's marks sway a count."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count import gold
from crossing_count.examples import export_examples
from crossing_count.util import slugify
from crossing_count.webapp import Setup
from crossing_count.wizard import Wizard, WizardError
from crossing_count.wizard_app import create_wizard_app

TEST_STORE = next(c for c in (f"T-{i}" for i in range(500)) if gold.split_of(c) == "test")


@pytest.fixture
def counted(clean_two_tile_video: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wizard:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))  # never the real settings
    w = Wizard(clean_two_tile_video["video"], clean_two_tile_video["dir"], tmp_path)
    w.set_mode("manual")
    w.set_marks(True)
    s = Setup(w.video, clean_two_tile_video["dir"])
    w.set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                        [{"picture": 0, "name": "CN-9-PB1", "include": True},
                         {"picture": 1, "name": "CN-9-R2", "include": False}])
    w.state["marks"] = False  # clean footage (the test names its camera instead of drawing it)
    w.set_direction("both")
    w.state["clock_start"] = "2026-09-12T11:30:00"  # from RetailNext's file name, normally
    w.set_store(code=TEST_STORE, operator="Alex")
    return w


def _count(w: Wizard, marks: list[tuple[float, str]]) -> None:
    dur = float(w.state["duration_s"])
    for frac, d in marks:
        w.manual_add("CN-9-PB1", round(dur * frac, 2), d)
    w.manual_watched("CN-9-PB1", 0.0, dur)
    w.manual_done()


def test_only_a_full_count_can_be_kept(counted: Wizard, tmp_path: Path) -> None:
    assert "Finish counting first." in gold.problems(counted.state)
    with pytest.raises(gold.GoldError, match="watched"):
        gold.save(counted.state, counted.run_dir, [], "", tmp_path)
    _count(counted, [(0.2, "in"), (0.6, "out")])
    assert gold.problems(counted.state) == []
    counted.state["marks"] = True  # the sensor's own tracks on the picture: its own tier
    assert gold.problems(counted.state) == [] and gold.footage_tier(counted.state) == "marked"
    assert gold.clip_id(counted.state).endswith("-marked")
    counted.state["marks"] = False
    names = [k["name"] for k in counted.counts()["checks"] if k["ok"]]
    assert "Footage of CN-9-PB1 watched" in names and "Counting finished" in names
    clip = gold.save(counted.state, counted.run_dir, ["groups"], "a note", tmp_path,
                     lighting="low", occlusion="some")
    assert (clip["split"], clip["crossings"], clip["tags"]) == ("test", 2, ["groups"])
    assert clip["conditions"]["lighting"] == "low" and clip["conditions"]["traffic"] in (
        "quiet", "normal", "busy", "heavy")
    assert (clip["rules"], clip["specification"]) == ({"children": "count", "staff": "count"}, "1.3")
    rec = json.loads((gold.folder(tmp_path) / f"{clip['id']}.json").read_text())
    first = rec["reviews"][0]["crossings"][0]
    assert (first["camera"], first["direction"]) == ("CN-9-PB1", "in")
    assert first["clock"].startswith("11:30:") and rec["video"]["width"] > 0
    with pytest.raises(gold.GoldError, match="Unknown tag"):
        gold.save(counted.state, counted.run_dir, ["sunny"], "", tmp_path)


def test_a_stores_set_is_fixed_by_its_code_everywhere() -> None:
    assert gold.split_of(f" {TEST_STORE.lower()} ") == "test"
    sets = [gold.split_of(f"S-{i}") for i in range(1000)]
    assert set(sets) == {"train", "validation", "test"}
    assert 150 < sets.count("test") < 250 and 150 < sets.count("validation") < 250


def test_what_counts_as_a_person_is_set_per_validation(counted: Wizard) -> None:
    counted.set_rules("exclude", "count")
    assert counted.state["rules"] == {"children": "exclude", "staff": "count"}
    assert counted.state["decisions"][-1]["action"] == "rules"
    with pytest.raises(WizardError, match="children"):
        counted.set_rules("sometimes", "count")


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
    counted.set_rules("exclude", "count")
    with pytest.raises(gold.GoldError, match="other rules"):
        gold.save(counted.state, counted.run_dir, [], "", tmp_path)
    counted.set_rules("count", "count")
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


def test_versions_are_frozen_and_a_test_score_names_one(counted: Wizard, tmp_path: Path) -> None:
    with pytest.raises(gold.GoldError, match="No gold clip"):
        gold.evaluate("development", tmp_path)
    _count(counted, [(0.2, "in")])
    clip = gold.save(counted.state, counted.run_dir, [], "", tmp_path)  # a test-set store
    with pytest.raises(gold.GoldError, match="Freeze the gold set first"):
        gold.evaluate("test", tmp_path)
    rel = gold.freeze(tmp_path, "first clip")
    assert rel["dataset_version"] == "gold_v1.1" and rel["clips"][0]["record"]["id"] == clip["id"]
    assert gold.freeze(tmp_path)["dataset_version"] == "gold_v1.1"  # unchanged: same version
    exp = gold.evaluate("test", tmp_path)
    assert exp["dataset"]["version"] == "gold_v1.1"
    (scored,) = exp["clips"]
    assert not scored["scored"] and "No automatic count" in scored["reason"]
    assert any(x.startswith("Insufficient sample") for x in exp["limits"])
    assert (gold.experiments_dir(tmp_path) / f"{exp['id']}.json").is_file()
    gold.save(counted.state, counted.run_dir, [], "changed", tmp_path)  # changed after freezing
    assert any("changed or gone since it was frozen" in c for c in gold.check(tmp_path))
    assert gold.overview(tmp_path)["version"] == "unreleased"
    client = TestClient(create_wizard_app(tmp_path / "sites", tmp_path, folders=[tmp_path]))
    seen = client.get("/api/gold").json()
    assert seen["test_scorings"] == 1 and seen["sets"]["test"]["clips"] == 1
    assert seen["releases"][0]["dataset_version"] == "gold_v1.1"
    assert client.post("/api/gold/freeze", json={"note": "second"}).json()["dataset_version"] == "gold_v1.2"
    assert client.get("/gold/").status_code == 200
    assert client.post("/api/gold/score", json={"which": "everything"}).status_code == 400


def test_the_team_folder_holds_everyones_work(counted: Wizard, tmp_path: Path) -> None:
    team = tmp_path / "team"
    _count(counted, [(0.2, "in")])
    clip = gold.save(counted.state, counted.run_dir, [], "", tmp_path / "me", shared=team)
    assert (gold.folder(team) / f"{clip['id']}.json").is_file()
    assert [r["id"] for r in gold.clips(tmp_path / "colleague", team)] == [clip["id"]]
    out = export_examples(counted, team)
    record = json.loads((team / "validations" / slugify(TEST_STORE)
                         / f"{slugify(Path(counted.state['filename']).stem)}.json").read_text())
    assert record["split"] == "test" and record["manual"]["counts"] and "decisions" in record
    lines = [json.loads(x) for x in (team / "index.jsonl").read_text().splitlines()]
    assert out["count"] == len(lines) and all(x["train_ok"] is False for x in lines)  # a test store


# ---- clean and marked footage ---------------------------------------------------------------

def test_clean_and_marked_counts_of_a_window_are_kept_apart(counted: Wizard, tmp_path: Path) -> None:
    _count(counted, [(0.2, "in"), (0.6, "out")])
    clean = gold.save(counted.state, counted.run_dir, [], "", tmp_path)
    assert clean["tier"] == "clean" and not clean["id"].endswith("-marked")
    assert clean["scoring"].startswith("Clean footage: kept for the final check")
    counted.state["marks"] = True  # the same window, counted on footage showing the marks
    marked = gold.save(counted.state, counted.run_dir, [], "", tmp_path)
    assert marked["id"] == clean["id"] + "-marked" and marked["window"] == clean["id"]
    assert marked["tier"] == "marked" and "not scored" in marked["scoring"]  # a test-set store
    assert marked["marked_why"] == [("Alex counted it on footage showing RetailNext's marks "
                                     "(the person counting said it shows RetailNext's marks)")]
    assert gold.find(counted.state, tmp_path)["id"] == marked["id"]  # type: ignore[index]
    counted.state["marks"] = False
    assert gold.find(counted.state, tmp_path)["id"] == clean["id"]  # type: ignore[index]
    assert gold.manifest(tmp_path)["tiers"] == {"clean": 1, "marked": 1}
    over = gold.overview(tmp_path)
    assert over["sets"]["test"] == {"clips": 2, "marked": 1, "stores": [TEST_STORE], "crossings": 4}
    effect = over["marks_effect"]
    assert effect["windows"] == 1 and effect["totals"]["agreed"] == 2
    assert effect["pairs"][0]["memory_warning"]  # the same person, the same day
    assert effect["sentence"].startswith("1 window(s) counted on both") and "at least 5" in effect["sentence"]
    # a clip kept before tiers, under the plain id, from marked footage: never mixed with clean
    path = gold.folder(tmp_path) / f"{clean['id']}.json"
    rec = json.loads(path.read_text())
    rec.pop("tier")
    rec["reviews"][0]["marked"] = True
    path.write_text(json.dumps(rec))
    with pytest.raises(gold.GoldError, match="before clean and marked counts were kept apart"):
        gold.save(counted.state, counted.run_dir, [], "", tmp_path)


def _clip(id_: str, split: str, tier: str) -> dict[str, Any]:
    return {"id": id_, "split": split, "store": {"code": id_[:2]}, "tags": [], "conditions": {},
            "tier": tier, "reviews": [{"reviewer": "Alex", "footage": {
                "clean": tier == "clean", "checked_in_picture": True}}]}


def test_marked_clips_are_scored_apart_and_never_in_the_test_set(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [_clip("W1", "train", "clean"), _clip("W1-marked", "train", "marked"),
            _clip("W2-marked", "validation", "marked"), _clip("T1-marked", "test", "marked"),
            _clip("T2", "test", "clean")]
    monkeypatch.setattr(gold, "clips", lambda root=None, shared=None: recs)

    def scored(rec: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
        counts = {"truth": 40, "pred": 38, "found": 36, "missed": 4, "false": 2}
        return {"id": rec["id"], "split": rec["split"], "tags": [], "groups": [], "store": "S1",
                "tier": gold.tier(rec), "window": gold.window_id(rec["id"]), "scored": True,
                "uncertain": 0, "agreement": None, "camera_hours": 0.25,
                "by_direction": {"in": counts}, "questions": 0, "listed": 0, "missed": 4,
                "misses_on_list": 0, "watch_s": 0.0,
                "detectors": [{"model": "yolo26m.pt", "backbone": "yolo", "sha256": "s"}]}

    monkeypatch.setattr(gold, "score_clip", scored)
    dev = gold.evaluate("development", tmp_path)
    assert dev["tiers"] == {"clean": 1, "marked": 2} and not dev["clean_only"]
    assert dev["totals"]["all"]["truth"] == 80  # W1 once (its clean count), and W2
    assert dev["by_tier"]["clean"]["all"]["truth"] == 40
    assert dev["by_tier"]["marked"]["all"]["truth"] == 80
    assert any("2 of the 3 scored clip(s) were counted on footage showing" in x for x in dev["limits"])
    assert any("1 window(s) counted on both" in x for x in dev["limits"])
    strict = gold.evaluate("development", tmp_path, clean_only=True)
    assert strict["tiers"] == {"clean": 1, "marked": 0}
    assert sorted(x["id"] for x in strict["left_out"]) == ["W1-marked", "W2-marked"]
    monkeypatch.setattr(gold, "manifest", lambda root=None, shared=None: {"content_hash": "h",
                                                                          "clips": []})
    monkeypatch.setattr(gold, "version_of", lambda current, root=None: "gold_v1.1")
    test = gold.evaluate("test", tmp_path)
    assert test["clean_only"] and [c["id"] for c in test["clips"]] == ["T2"]
    assert test["left_out"] == [{"id": "T1-marked", "tier": "marked", "why": [
        "counted on footage showing RetailNext's marks: the test set is clean footage only"]}]
    brief = gold.experiments(tmp_path)
    assert {(e["set"], e["tiers"]["marked"]) for e in brief} == {("development", 2),
                                                                  ("development", 0), ("test", 0)}


def _counted(id_: str, tier: str, times: list[float], who: str, day: int,
             system: int | None = None, dirs: tuple[str, ...] = ("in",)) -> dict[str, Any]:
    return {"id": id_, "split": "train", "store": {"code": "S1"},
            "period": {"start": "2026-09-12T11:30:00", "end": "2026-09-12T11:45:00"},
            "cameras": [{"sensor": "C1"}], "dirs": list(dirs), "tier": tier,
            "rules": {"children": "count", "staff": "count"},
            "reviews": [{"reviewer": who, "at": f"2026-09-{day:02d}T10:00:00+10:00",
                         "footage": {"clean": tier == "clean", "checked_in_picture": True},
                         "system_counts": None if system is None else {
                             "system": "RetailNext", "counts": {"in": system}},
                         "crossings": [{"camera": "C1", "t": t, "direction": dirs[0]}
                                       for t in times]}]}


def test_windows_counted_both_ways_show_how_far_the_marks_move_a_count(
        monkeypatch: pytest.MonkeyPatch) -> None:
    recs = [
        # counted on marked footage, the count lost a crossing, towards RetailNext's 3
        _counted("W1", "clean", [10, 20, 30, 40], "Alex", 1),
        _counted("W1-marked", "marked", [10, 20, 30], "Sam", 9, system=3),
        # the same person twice on one day: agrees, but memory may be why
        _counted("W2", "clean", [5, 15], "Alex", 2), _counted("W2-marked", "marked", [5, 15], "Alex", 2),
        _counted("W3-marked", "marked", [1], "Sam", 3),  # no clean count: not a pair
        _counted("W4", "clean", [1], "Alex", 4),
        _counted("W4-marked", "marked", [1], "Sam", 12, dirs=("out",)),  # other traffic
    ]
    monkeypatch.setattr(gold, "clips", lambda root=None, shared=None: recs)
    effect = gold.marks_effect()
    assert effect["windows"] == 2 and len(effect["pairs"]) == 3
    w1, w2, w4 = effect["pairs"]
    assert (w1["agreed"], w1["only_clean"], w1["only_marked"]) == (3, 1, 0)
    assert w1["by_direction"]["in"] == {"clean": 4, "marked": 3, "shift": -1, "system": 3,
                                        "towards_system": "towards"}
    assert not w1["memory_warning"] and w1["days_apart"] == 8.0
    assert w2["memory_warning"] and w2["by_direction"]["in"]["towards_system"] is None
    assert not w4["comparable"] and "not counted the same way" in w4["reason"]
    t = effect["totals"]
    assert (t["agreed"], t["only_clean"], t["shift"], t["towards_system"]) == (5, 1, {"in": -1, "out": 0}, 1)
    assert effect["memory_warnings"] == 1 and "at least 5" in effect["sentence"]
    for k in range(3, 6):  # five windows: then it says what it found
        recs += [_counted(f"X{k}", "clean", [7], "Alex", 1), _counted(f"X{k}-marked", "marked", [7], "Sam", 20)]
    said = gold.marks_effect()["sentence"]
    assert said.startswith("Across 5 windows counted both ways") and "towards the system's number 1" in said
