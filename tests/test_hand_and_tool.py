"""Counting by hand on RetailNext's marked footage while the tool counts the clean twin:
what the tool found stays sealed until the count is done, then the two are put side by side
and each difference can be given its cause."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from test_wizard import copy_outputs, wait

from crossing_count import retailnext as rn
from crossing_count.webapp import Setup
from crossing_count.wizard import Wizard, WizardError

WINDOW = "2026-09-12-113000 AEST to 2026-09-12-114500 AEST"


@pytest.fixture
def pair(two_tile_video: dict[str, Any], clean_two_tile_video: dict[str, Any], tmp_path: Path,
         monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The same window downloaded twice, as the download step now offers: clean and marked."""
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    folder = tmp_path / "footage"
    folder.mkdir()
    marked = folder / f"Export - CN-9 marked - {WINDOW}.mp4"
    clean = folder / f"Export - CN-9 - {WINDOW}.mp4"
    shutil.copy(two_tile_video["video"], marked)
    shutil.copy(clean_two_tile_video["video"], clean)
    for path, other, marks in ((marked, clean, True), (clean, marked, False)):
        rn.remember_download(path, {"code": "CN-9", "name": "CN-9 Test store", "marks": marks,
                                    "cameras": ["CN-9-PB1", "CN-9-R2"], "twin": other.name,
                                    "start": "2026-09-12T11:30:00+10:00",
                                    "end": "2026-09-12T11:45:00+10:00"})
    return {"marked": marked, "clean": clean, "sites": two_tile_video["dir"], "root": tmp_path}


def _counting(pair: dict[str, Any], commands: Any = None) -> Wizard:
    w = Wizard(pair["marked"], pair["sites"], pair["root"], commands)
    w.set_mode("manual")
    s = Setup(w.video, pair["sites"])
    w.set_cameras(s.existing(), [t.as_dict() for t in s.tiles])  # CAM-A and CAM-B, as drawn
    w.set_direction("in")
    return w


def test_the_tool_counts_the_clean_twin_while_a_person_counts_the_marked_one(
        pair: dict[str, Any], review_run_dir: Path) -> None:
    ran: list[str] = []

    def commands(w: Wizard) -> list[list[str]]:
        ran.append(w.count_video().name)
        return copy_outputs(review_run_dir)(w)

    w = _counting(pair, commands)
    assert w.twin() == pair["clean"] and w.footage_marked()  # its name says it shows the marks
    assert w.count_video() == pair["clean"]  # nothing of the sensor's in the tool's pictures
    w.start()
    wait(w)
    assert ran == [pair["clean"].name]
    job = w.job_status()
    assert job["status"] == "done" and job["sealed"] and "results" not in job
    assert w.sealed() and w.public()["twin"] == pair["clean"].name
    assert w.tool_vs_hand() is None  # nothing of the tool's until the count is finished


def test_a_count_by_hand_alone_still_has_nothing_to_run(pair: dict[str, Any]) -> None:
    w = Wizard(pair["clean"], pair["sites"], pair["root"])  # no marked twin to count by hand
    w.set_mode("manual")
    assert w.count_video() == pair["clean"]
    with pytest.raises(WizardError, match="Download the same window clean as well"):
        w.start()


def test_the_two_counts_go_side_by_side_and_each_difference_gets_its_cause(
        pair: dict[str, Any], review_run_dir: Path) -> None:
    w = _counting(pair, copy_outputs(review_run_dir))
    w.start()
    wait(w)
    found = [c for c in (w._read(w.state["cameras"][0], "candidates").get("candidates") or [])
             if c["direction"] == "in"]
    assert len(found) >= 2, "the prepared run must have crossings to compare against"
    dur = float(w.state["duration_s"])
    w.manual_add("CAM-A", round(float(found[0]["t_seconds"]), 2), "in")  # both saw this one
    w.manual_add("CAM-A", round(dur * 0.9, 2), "in")  # only the person saw this one
    w.manual_watched("CAM-A", 0.0, dur)
    w.set_sensor({"in": 1})  # RetailNext counted fewer than the person did
    w.manual_done()

    got = w.tool_vs_hand()
    assert got is not None and not w.sealed()
    assert got["video"] == pair["clean"].name and got["offset_s"] == 0.0
    kinds = [r["kind"] for r in got["rows"]]
    assert "missed" in kinds and "false" in kinds  # each way round
    (mine,) = [r for r in got["rows"] if r["kind"] == "missed"]
    assert mine["what"] == "you counted it, the tool did not" and mine["camera"] == "CAM-A"
    extra = [r for r in got["rows"] if r["kind"] == "false"]
    # the tool alone saw these, and RetailNext counted no more than the person: worth a look
    assert all(r["alone"] for r in extra) and got["alone"] == len(extra)
    assert got["totals"]["all"]["truth"] == 2 and got["totals"]["all"]["found"] == 1

    w.set_diagnosis(extra[0]["key"], "no_track", "RetailNext never boxed her")
    again = w.tool_vs_hand()
    assert again is not None
    (tagged,) = [r for r in again["rows"] if r["key"] == extra[0]["key"]]
    assert tagged["diagnosis"]["cause"] == "no_track"
    assert tagged["diagnosis"]["note"] == "RetailNext never boxed her"
    assert "no_track" in again["causes"] and "RetailNext never tracked" in again["causes"]["no_track"]
    with pytest.raises(WizardError, match="unknown cause"):
        w.set_diagnosis(extra[0]["key"], "made-up")
    w.set_diagnosis(extra[0]["key"], "")  # taken back
    assert [r for r in (w.tool_vs_hand() or {})["rows"] if r["diagnosis"]] == []
    assert [d["action"] for d in w.state["decisions"] if d["action"] == "diagnosed"]
