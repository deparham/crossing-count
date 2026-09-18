"""Counting systems' numbers in one form: RetailNext's API, or a CSV from any counter."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count import sensors
from crossing_count.webapp import Setup
from crossing_count.wizard import Wizard, WizardError
from crossing_count.wizard_app import create_wizard_app


def test_a_csv_in_either_form_reads_the_same() -> None:
    wide = "﻿Camera,Start,End,In,Out\nCN-1,2026-09-12 11:30,2026-09-12 11:45,15,13\n"
    long = ("start,end,direction,count,system\n12/09/2026 11:30,12/09/2026 11:45,in,15,Xovis\n"
            "2026-09-12T11:30:00+10:00,2026-09-12T11:45:00+10:00,out,13,\n")
    a, b = sensors.read_csv(wide, "cn1.csv"), sensors.read_csv(long, "export.csv")
    assert [(c.direction, c.count) for c in a.counts] == [("in", 15), ("out", 13)]
    assert [(c.direction, c.count) for c in b.counts] == [("in", 15), ("out", 13)]
    assert a.counts[0].camera == "CN-1" and b.counts[0].camera == ""
    assert b.counts[1].start == datetime.fromisoformat("2026-09-12T11:30")  # zone dropped: local
    assert (a.system, b.system) == ("cn1", "Xovis")


@pytest.mark.parametrize(("text", "said"), [
    ("start,end\n", "needs columns"),
    ("start,end,in\n2026-09-12 11:30,2026-09-12 11:45,many\n", "Line 2"),
    ("start,end,in\nnoon,2026-09-12 11:45,1\n", "not a date"),
    ("start,end,in\n2026-09-12 11:45,2026-09-12 11:30,1\n", "end is not after"),
    ("start,end,direction,count\n2026-09-12 11:30,2026-09-12 11:45,sideways,1\n", "not in or out"),
    ("start,end,in\n2026-09-12 11:30,2026-09-12 11:45,-2\n", "whole number"),
])
def test_a_bad_csv_says_where(text: str, said: str) -> None:
    with pytest.raises(sensors.SensorError, match=said):
        sensors.read_csv(text)


def test_retailnexts_answer_becomes_interval_counts() -> None:
    got = {"day": "2026-09-12", "store": "Test", "subscription": "rag",
           "cameras": {"CN-1": [{"start": "11:45", "finish": "12:00", "in": 2, "out": 3,
                                 "validity": "imputed"}]}}
    data = sensors.from_retailnext(got)
    assert data.system == "RetailNext" and data.detail["subscription"] == "rag"
    assert data.counts[0] == sensors.IntervalCount(datetime.fromisoformat("2026-09-12T11:45"),
                                                   datetime.fromisoformat("2026-09-12T12:00"), "in",
                                                   2, "CN-1", "imputed")


@pytest.fixture
def named(two_tile_video: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wizard:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    w = Wizard(two_tile_video["video"], two_tile_video["dir"], tmp_path)
    w.set_mode("manual")
    w.set_marks(True)
    s = Setup(w.video, two_tile_video["dir"])
    w.set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                        [{"picture": 0, "name": "CN-9-PB1", "include": True},
                         {"picture": 1, "name": "CN-9-R2", "include": True}])
    w.set_direction("both")
    w.state["clock_start"] = "2026-09-12T11:59:50"  # the footage crosses 12:00
    return w


def _csv(w: Wizard, cams: list[str], halves: bool = False) -> str:
    lines = ["camera,start,end,in,out"]
    for i in w.intervals():
        s = datetime.fromisoformat(i["start"])
        parts = [(s, s + timedelta(minutes=7)), (s + timedelta(minutes=7), s + timedelta(minutes=15))] \
            if halves else [(s, s + timedelta(minutes=15))]
        for cam in cams:
            lines += [f"{cam},{a:%Y-%m-%d %H:%M},{b:%Y-%m-%d %H:%M},{1 if halves else 2},1"
                      for a, b in parts]
    return "\n".join(lines) + "\n"


def test_another_counters_csv_fills_in_the_numbers(named: Wizard) -> None:
    keys = [i["key"] for i in named.intervals()]
    assert len(keys) == 2
    data = sensors.CsvAdapter(_csv(named, ["CN-9-PB1", "CN-9-R2", "CN-7-X"]), "xovis.csv").fetch(
        ["CN-9-PB1", "CN-9-R2"], datetime.fromisoformat("2026-09-12T11:59"),
        datetime.fromisoformat("2026-09-12T12:01"))
    assert named.use_sensor(data) == []
    assert named.state["sensor_intervals"] == {k: {"in": 4, "out": 2} for k in keys}
    assert named.state["sensor_cameras"]["CN-9-R2"] == {"in": 4, "out": 2}
    assert named.state["sensor_source"]["system"] == "xovis"
    assert "imported from xovis.csv" in named._system_line()
    finer = sensors.read_csv(_csv(named, ["CN-9-PB1", "CN-9-R2"], halves=True), "fine.csv")
    named.use_sensor(finer)  # 7- and 8-minute rows add up to each interval
    assert named.state["sensor_intervals"] == {k: {"in": 4, "out": 4} for k in keys}
    one = sensors.read_csv(_csv(named, ["CN-9-PB1"]), "one.csv")
    with pytest.raises(WizardError, match="no numbers for CN-9-R2"):
        named.use_sensor(one)
    gap = sensors.read_csv("start,end,in,out\n2026-09-12 11:45,2026-09-12 11:50,1,1\n", "gap.csv")
    with pytest.raises(WizardError, match="gave no number"):
        named.use_sensor(gap)


def test_a_video_a_moment_too_long_still_gets_the_sensors_numbers(named: Wizard) -> None:
    named.state["clock_start"] = "2026-09-12T11:45:00"
    named.state["duration_s"] = 900.1  # RetailNext's exports run a fraction of a second over
    assert [i["key"] for i in named.intervals()] == ["11:45"]  # not 12:00, which nobody counted
    named.use_sensor(sensors.read_csv(_csv(named, ["CN-9-PB1", "CN-9-R2"]), "x.csv"))
    assert named.state["sensor"] == {"in": 4, "out": 2}
    cov = named.coverage()
    assert cov is not None and cov["full"] and cov["pct"] == 100.0
    assert named.coverage_words() is None
    ok = [k for k in named.counts()["checks"] if k["name"].startswith("Footage covers")]
    assert [k["ok"] for k in ok] == [True]
    named.state["duration_s"] = 891.3  # seconds short of the window: beneath the counts' noise
    assert (named.coverage() or {})["full"] and named.coverage_words() is None


def test_footage_short_of_the_sensors_interval_says_so(named: Wizard) -> None:
    named.state["clock_start"] = "2026-09-12T11:45:00"
    named.state["duration_s"] = 810.0  # 13.5 of the 15 minutes the sensor's number covers
    named.use_sensor(sensors.read_csv(_csv(named, ["CN-9-PB1", "CN-9-R2"]), "x.csv"))
    cov = named.coverage()
    assert cov is not None and not cov["full"]
    assert (cov["pct"], cov["missing_s"], cov["period_s"]) == (90.0, 90.0, 900)
    said = named.coverage_words() or ""
    assert "its number covers 15 minutes, the footage 13.5 of them (90.0%)" in said
    assert "1.5 minutes not in the footage" in said
    (check,) = [k for k in named.counts()["checks"] if k["name"].startswith("Footage covers")]
    assert check["ok"] is False and check["detail"] == "13.5 of 15 minutes (90.0%)"


def test_the_result_is_kept_as_data(named: Wizard) -> None:
    named.state["clock_start"] = "2026-09-12T11:45:00"  # one interval: a total for the footage
    named.use_sensor(sensors.read_csv(
        "start,end,in,out\n2026-09-12 11:45,2026-09-12 12:00,3,2\n", "total.csv"))
    named.manual_add("CN-9-PB1", 1.0, "in")
    r = named.result()
    assert r["system"] == "total" and r["source"] == "CSV file" and r["status"] == "incomplete"
    assert [(x["direction"], x["truth"], x["system"]) for x in r["rows"]] == [("in", 1, 3),
                                                                             ("out", 0, 2)]
    assert r["metrics"]["in"]["error"] == 2


def test_the_sensor_results_page_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    client = TestClient(create_wizard_app(tmp_path / "sites", tmp_path, folders=[tmp_path]))
    assert client.get("/sensors/").status_code == 200
    assert client.get("/api/sensors").json()["summary"] is None


def test_the_report_says_the_periods_do_not_match(named: Wizard) -> None:
    named.state["clock_start"] = "2026-09-12T11:45:00"
    named.state["duration_s"] = 810.0
    named.use_sensor(sensors.read_csv(_csv(named, ["CN-9-PB1", "CN-9-R2"]), "x.csv"))
    named.set_store(name="Lismore", code="CN-9", operator="Alex")
    data = named.report_data(named.counts(), named.verified_rows(), Path("frame.jpg"), "", [])
    assert any("the footage 13.5 of them (90.0%)" in x for x in data["caveats"])  # on page 1
