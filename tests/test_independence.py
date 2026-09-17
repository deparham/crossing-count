"""Independence of the sensor: clean footage, proved in the picture; and a counting line
that is where the sensor counts, on the camera it belongs to."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count import correspondence as co
from crossing_count import gold, independence
from crossing_count import retailnext as rn
from crossing_count import validation as v
from crossing_count.webapp import Draft, Setup
from crossing_count.wizard import Wizard, WizardError
from crossing_count.wizard_app import create_wizard_app

TRAIN_STORE = next(c for c in (f"A-{i}" for i in range(500)) if gold.split_of(c) == "train")
LINE_A = [[100.0, 200.0], [320.0, 190.0], [540.0, 200.0]]  # over the fixture's burned-in line
INSIDE = [320.0, 300.0]
NODES: list[dict[str, Any]] = [
    {"uuid": "s1", "location_type": "store", "store_id": "CN-9", "name": "Test store",
     "time_zone": "Australia/Sydney"},
    {"uuid": "e1", "location_type": "entrance", "name": "CAM-A", "parent_uuid": "s1"},
    {"uuid": "e2", "location_type": "entrance", "name": "CAM-B", "parent_uuid": "s1"},
    {"uuid": "v1", "type": "video", "name": "CAM-A", "parent_uuid": "e1"},
    {"uuid": "v2", "type": "video", "name": "CAM-B", "parent_uuid": "e2"},
]


def test_what_the_picture_shows_beats_what_anyone_says(
        clean_two_tile_video: dict[str, Any], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    w = Wizard(clean_two_tile_video["video"], clean_two_tile_video["dir"], tmp_path)
    w.set_mode("manual")
    w.set_marks(False)  # "it is clean" - but RetailNext's lines are in the picture
    assert w.footage()["clean"]  # nothing has looked at the pictures yet
    w.note_marks_detected([{"marks": True, "segments": 9, "length_px": 1400.0},
                           {"marks": False, "segments": 0, "length_px": 0.0}])
    f = w.footage()
    assert not f["clean"] and f["why_marked"] == ["RetailNext's lines are visible in picture 1"]
    assert w.footage_marked() and f["obtained"] == "by_hand"
    assert gold.footage_tier(w.state) == "marked"  # a count on it is a marked gold clip


def test_a_check_on_marked_footage_is_not_independent_either() -> None:
    # Perri Cutten Armadale 392: an automatic count checked by a person on marked footage
    marked = independence.facts("Export - 392 marked - 2026-09-13-124500 AEST to x.mp4",
                                {"marks": True}, None, None)
    assert not marked["clean"] and len(marked["why_marked"]) == 2
    assert marked["obtained_words"] == "downloaded from RetailNext with its marks"
    clean = independence.facts("Export - CN-9 - x.mp4", {"marks": False}, False,
                               [{"marks": False}, {"marks": False}])
    assert clean["clean"] and clean["checked_in_picture"]
    assert independence.facts("anything.mp4", None, True, None)["why_marked"] == [
        "the person counting said it shows RetailNext's marks"]


def test_earlier_results_are_found_and_kept_out_of_the_comparison(tmp_path: Path) -> None:
    p = tmp_path / "runs" / "392" / "wizard" / "state.json"
    p.parent.mkdir(parents=True)
    result = {"fingerprint": "f392", "system": "RetailNext", "status": "complete", "marked": False,
              "store": {"code": "392"}, "cameras": ["C1"], "duration_s": 900,
              "rows": [{"interval": "whole footage", "direction": "in", "truth": 11, "system": 9,
                        "covered_s": 900, "cameras": 1}], "metrics": None}
    p.write_text(json.dumps({
        "fingerprint": "f392", "mode": "auto", "marks": True, "video": str(tmp_path / "gone.mp4"),
        "filename": "Export - 392 marked - 2026-09-13-124500 AEST to x.mp4",
        "store": {"code": "392", "operator": "Alex"}, "result": result}))
    assert v.overview(tmp_path)["validations"][0]["store"] == "392"  # counted, before the audit
    (found,) = independence.audit(tmp_path, look=False)
    assert found["store"] == "392" and found["mode"] == "auto"
    assert "its name says it shows RetailNext's marks" in found["why"]
    independence.reclassify(tmp_path, [found], by="Alex")
    after = v.overview(tmp_path)
    assert after["validations"] == [] and after["excluded"][0]["store"] == "392"
    assert "found on audit" in after["excluded"][0]["excluded"][0]
    assert independence.audit(tmp_path, look=False) == []  # already recorded


def test_provisional_clips_are_kept_listed_and_left_out_of_scoring(
        clean_two_tile_video: dict[str, Any], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    w = Wizard(clean_two_tile_video["video"], clean_two_tile_video["dir"], tmp_path)
    w.set_mode("manual")
    w.set_marks(True)
    s = Setup(w.video, clean_two_tile_video["dir"])
    w.set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                        [{"picture": 0, "name": "CN-9-PB1", "include": True},
                         {"picture": 1, "name": "CN-9-R2", "include": False}])
    w.state["marks"] = False
    w.set_direction("in")
    w.state["clock_start"] = "2026-09-12T11:30:00"
    w.set_store(code=TRAIN_STORE, operator="Alex")
    dur = float(w.state["duration_s"])
    w.manual_add("CN-9-PB1", 5.0, "in")
    w.manual_watched("CN-9-PB1", 0.0, dur)
    w.manual_done()
    clip = gold.save(w.state, w.run_dir, [], "", tmp_path)
    assert clip["provisional"] == [] and gold.manifest(tmp_path)["provisional"] == []
    path = gold.folder(tmp_path) / f"{clip['id']}.json"
    rec = json.loads(path.read_text())
    assert rec["reviews"][0]["footage"]["clean"] and rec["reviews"][0]["footage"]["checked_in_picture"]
    rec["reviews"][0]["footage"]["checked_in_picture"] = False  # kept before that check
    path.write_text(json.dumps(rec))
    assert gold.provisional(rec)[0].startswith("Alex's footage was not checked")
    assert gold.manifest(tmp_path)["provisional"] == [clip["id"]]
    with pytest.raises(gold.GoldError, match="1 clip.s. left out"):
        gold.evaluate("development", tmp_path)  # the only clip is not known to be clean
    rec.pop("tier")
    rec["reviews"][0]["marked"] = True  # as an older clip, kept from marked footage, would be
    path.write_text(json.dumps(rec))
    assert gold.tier(rec) == "marked" and gold.provisional(rec) == []  # a tier, not a doubt
    assert gold.describe(rec)["marked_why"][0].startswith("Alex counted it on footage showing")


def _draft(**over: Any) -> Draft:
    base: dict[str, Any] = {"picture": 0, "site": "SYN", "sensor": "CAM-A", "line": LINE_A,
                            "inside": INSIDE}
    return Draft(**{**base, **over})


def test_a_line_drawn_over_the_sensors_line_is_calibrated_and_finds_its_camera(
        two_tile_video: dict[str, Any], clean_two_tile_video: dict[str, Any],
        tmp_path: Path) -> None:
    sites = tmp_path / "sites"
    sites.mkdir()
    marked = Setup(two_tile_video["video"], sites)
    got = marked.evaluate(_draft())
    assert got["ok"] and got["correspondence"]["method"] == "calibrated"
    assert got["correspondence"]["line_match"] >= co.MIN_LINE_MATCH
    saved = marked.save(_draft())
    assert Path(saved["reference"]).is_file()

    clean = Setup(clean_two_tile_video["video"], sites)  # another export, no marks in it
    clean.names = ["CAM-A", "CAM-B"]
    (cfg,) = clean.existing()
    assert (cfg["picture"], cfg["confident"]) == (0, True)
    assert cfg["how"] == "its camera name and its picture" and cfg["alignment"]["aligned"]
    assert cfg["correspondence"]["method"] == "calibrated"
    by_eye = clean.evaluate(_draft(sensor="CAM-B", picture=1))
    assert by_eye["correspondence"]["method"] == "by_eye"


def test_a_camera_matched_by_its_name_alone_blocks_the_run(
        two_tile_video: dict[str, Any], clean_two_tile_video: dict[str, Any], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    sites = tmp_path / "sites"
    sites.mkdir()
    clean = Setup(clean_two_tile_video["video"], sites)
    saved = clean.save(_draft())  # drawn by eye on clean footage
    Path(saved["reference"]).unlink()  # and no picture kept of what it was drawn on
    other = Setup(two_tile_video["video"], sites)
    other.names = ["CAM-A", "CAM-B"]
    (cfg,) = other.existing()
    assert cfg["picture"] == 0 and cfg["confident"] is False
    assert cfg["how"].startswith("its camera name only")
    w = Wizard(two_tile_video["video"], sites, tmp_path)
    w.set_mode("auto")
    w.set_cameras(other.existing(), [t.as_dict() for t in other.tiles])
    w.set_direction("in")
    (cam,) = w.state["cameras"]
    assert cam["confident"] is False and w.unconfirmed()
    with pytest.raises(WizardError, match="confirm it on the Cameras step"):
        w.start()
    with pytest.raises(WizardError, match="confirm it on the Cameras step"):
        w.make_report(None)
    w.set_store(operator="Alex")
    w.confirm_camera(0)
    assert w.unconfirmed() == [] and w.state["cameras"][0]["confirmed_by"] == "Alex"
    assert w.state["decisions"][-1]["action"] == "camera_confirmed"
    with pytest.raises(WizardError, match="Run the count first"):
        w.make_report(None)  # the camera no longer blocks it


def test_the_wizard_fetches_a_marked_minute_to_calibrate_on(
        two_tile_video: dict[str, Any], clean_two_tile_video: dict[str, Any], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    conn = rn.Connection("acme", "a", "b")
    monkeypatch.setattr(rn, "connections", lambda: [conn])
    monkeypatch.setattr(rn, "locations", lambda c, types=None: NODES)
    asked: list[tuple[list[str], bool]] = []

    def fake_start(c: Any, channels: list[str], start: Any, end: Any, marks: bool) -> str:
        asked.append((channels, marks))
        return "x1"

    monkeypatch.setattr(rn, "start_export", fake_start)
    monkeypatch.setattr(rn, "export_status", lambda c, i: ("ready", "https://s/f.mp4"))
    monkeypatch.setattr(rn, "download", lambda url, dest, progress=None: (
        shutil.copy(two_tile_video["video"], dest), dest)[1])  # the marked minute
    footage = tmp_path / "footage"
    footage.mkdir()
    clean = footage / ("Export - CN-9 - 2026-09-12-113000 AEST to 2026-09-12-114500 AEST.mp4")
    shutil.copy(clean_two_tile_video["video"], clean)
    rn.remember_download(clean, {"subscription": "acme", "code": "CN-9", "name": "Test store",
                                 "store_uuid": "s1", "time_zone": "Australia/Sydney",
                                 "cameras": ["CAM-A", "CAM-B"], "marks": False,
                                 "start": "2026-09-12T11:30:00+10:00",
                                 "end": "2026-09-12T11:45:00+10:00"})
    client = TestClient(create_wizard_app(tmp_path / "sites", tmp_path, folders=[footage]))
    opened = client.post("/api/open", json={"path": str(clean)})
    assert opened.status_code == 200 and opened.json()["marks_detected"] is not None
    drawn = client.get("/api/drawn").json()
    assert drawn["can_calibrate"] and not any(p["marks"] for p in drawn["pictures"])
    assert client.post("/api/calibrate", json={}).status_code == 200
    job: dict[str, Any] = {}
    for _ in range(200):
        job = client.get("/api/calibrate").json()
        if job["state"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert job["state"] == "done", job.get("message")
    assert asked == [(["v1", "v2"], True)]  # the same cameras, this time with RetailNext's marks
    name = Path(job["path"]).name  # the store's clock, as RetailNext names it; no ":" (Windows)
    assert name.startswith("Export - CN-9 marked - 2026-09-12-113000 AEST") and ":" not in name
    info = client.get(f"{job['draw_url']}api/info")
    assert info.status_code == 200 and len(info.json()["pictures"]) == 2
