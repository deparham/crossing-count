"""Manual counting: key presses, watched stretches, intervals, sensor comparison, export."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count.export import CSV_COLUMNS
from crossing_count.manual import (
    ManualError,
    ManualSession,
    intervals_for,
    merge_ranges,
    unwatched_ranges,
)
from crossing_count.manual_app import create_manual_app
from crossing_count.video import fingerprint

START = datetime(2026, 9, 12, 11, 30, 0)  # noqa: DTZ001 - wall clock as in the filename


@pytest.fixture
def session(two_tile_video: dict[str, Any], tmp_path: Path) -> ManualSession:
    return ManualSession(two_tile_video["video"], tmp_path, "Pat", start=START)


def test_watched_stretches_merge_and_gaps_are_listed() -> None:
    w = merge_ranges([(10.0, 20.0), (20.3, 30.0), (0.0, 5.0), (4.0, 6.0)])
    assert w == [[0.0, 6.0], [10.0, 30.0]]
    assert unwatched_ranges(w, 60.0) == [[6.0, 10.0], [30.0, 60.0]]
    assert unwatched_ranges([[0.0, 59.5]], 60.0) == []  # a tail under a second is not a gap


def test_intervals_follow_the_sensors_quarter_hours() -> None:
    (only,) = intervals_for(START, 900.0)
    assert only["key"] == "11:30" and only["full"] is True
    a, b = intervals_for(datetime(2026, 9, 12, 11, 37), 1200.0)  # noqa: DTZ001
    assert (a["key"], a["covered_s"], a["full"]) == ("11:30", 480.0, False)
    assert (b["key"], b["covered_s"], b["full"]) == ("11:45", 720.0, False)
    assert intervals_for(None, 60.0)[0]["key"] == "all"


def test_counts_survive_a_restart_and_undo_takes_the_last(session: ManualSession,
                                                          two_tile_video: dict[str, Any],
                                                          tmp_path: Path) -> None:
    assert [c["name"] for c in session.state["cameras"]] == ["Camera 1", "Camera 2"]
    first = session.add(0, 21.5, "in")
    session.add(0, 40.0, "out")
    session.add(1, 31.2, "in")
    assert session.delete(first["id"]) and not session.delete(first["id"])
    again = ManualSession(two_tile_video["video"], tmp_path)
    assert [(c["camera"], c["t"], c["direction"]) for c in again.state["counts"]] == [
        (0, 40.0, "out"), (1, 31.2, "in")]
    assert again.state["operator"] == "Pat"
    removed = again.undo(0)
    assert removed is not None and removed["t"] == 40.0
    assert again.undo(0) is None
    assert again.summary()["total"] == {"in": 1, "out": 0}


def test_bad_presses_are_refused(session: ManualSession) -> None:
    for camera, t, direction in ((5, 1.0, "in"), (0, 1.0, "sideways"), (0, -3.0, "in"),
                                 (0, 999.0, "in")):
        with pytest.raises(ManualError):
            session.add(camera, t, direction)
    with pytest.raises(ManualError):
        session.set_sensor("09:00", 0, "in", 3)  # the video does not cover 09:00


def test_sensor_comparison_per_camera_and_for_all_cameras(session: ManualSession) -> None:
    for t in (10.0, 20.0, 30.0):
        session.add(0, t, "in")
    session.add(1, 15.0, "out")
    for cam, d, v in ((0, "in", 2), (1, "in", 1), (0, "out", 0), (1, "out", 1)):
        session.set_sensor("11:30", cam, d, v)
    (iv,) = session.summary()["intervals"]
    cam0 = iv["cameras"][0]
    assert cam0["in"] == 3 and cam0["comparison"]["in"]["accuracy_pct"] == pytest.approx(66.7)
    assert iv["all"]["sensor"] == {"in": 3, "out": 1}  # the cameras' numbers added up
    assert iv["all"]["sensor_source"]["in"] == "sum of cameras"
    assert iv["all"]["comparison"]["in"]["accuracy_pct"] == 100.0
    session.set_sensor("11:30", None, "in", 4)  # RetailNext's combined figure wins
    (iv,) = session.summary()["intervals"]
    assert iv["all"]["sensor"]["in"] == 4 and iv["all"]["sensor_source"]["in"] == "entered"
    assert iv["all"]["comparison"]["in"]["error"] == 1
    session.set_sensor("11:30", None, "in", None)
    assert session.summary()["intervals"][0]["all"]["sensor"]["in"] == 3


def test_unwatched_stretches_and_partial_intervals_are_warned(session: ManualSession) -> None:
    session.watched(0, 0.0, 30.0)
    session.watched(0, 29.8, 59.9)
    session.watched(1, 0.0, 20.0)
    s = session.summary()
    assert s["cameras"][0]["unwatched"] == [] and s["cameras"][0]["watched_pct"] > 99
    assert s["cameras"][1]["unwatched"][0][0] == 20.0
    assert session.state["positions"]["1"] == 20.0
    assert any("Camera 2" in w and "not watched" in w for w in s["warnings"])
    assert any("covers only" in w for w in s["warnings"])  # 60 s of a 15-minute interval


def test_csv_and_report(session: ManualSession, tmp_path: Path) -> None:
    session.add(0, 21.5, "in")
    session.add(0, 3.0, "out")
    session.rename_camera(0, "CN-1")
    session.set_meta(site="Tweed Heads")
    out = tmp_path / "out"
    assert {p.name for p in session.export(out)} == {"CN-1.csv", "Camera_2.csv", "summary.json",
                                                     "report.html"}
    rows = list(csv.reader(io.StringIO((out / "CN-1.csv").read_text())))
    header = {r[0]: r[1] for r in rows[: rows.index([])] if len(r) >= 2}
    assert header["sensor"] == "CN-1" and header["site"] == "Tweed Heads"
    assert header["rule"] == "manual count" and header["operator"] == "Pat"
    table = rows[rows.index([]) + 1:]
    assert table[0] == CSV_COLUMNS
    assert [r[2:4] for r in table[1:]] == [["11:30:03", "out"], ["11:30:21", "in"]]
    report = (out / "report.html").read_text()
    assert "Manual count" in report and "http" not in report
    assert json.loads((out / "summary.json").read_text())["summary"]["total"]["in"] == 1


def test_camera_names_and_pictures_come_from_gate_layout(two_tile_video: dict[str, Any],
                                                         tmp_path: Path) -> None:
    pictures = [{"index": i, "x0": 640 * i, "y0": 60, "x1": 640 * (i + 1), "y1": 540,
                 "header_px": 16} for i in range(2)]
    (tmp_path / "layout.json").write_text(json.dumps({
        "fingerprint": fingerprint(two_tile_video["video"]), "pictures": pictures,
        "cameras": [{"sensor": "CAM-B", "picture": 1}]}))
    s = ManualSession(two_tile_video["video"], tmp_path)
    assert [c["name"] for c in s.state["cameras"]] == ["Camera 1", "CAM-B"]
    assert s.state["cameras"][1]["tile"]["x0"] == 640


def test_count_page_api(session: ManualSession) -> None:
    client = TestClient(create_manual_app(session))
    assert "Manual count" in client.get("/").text
    st = client.get("/api/state").json()
    assert len(st["cameras"]) == 2 and st["summary"]["total"] == {"in": 0, "out": 0}
    r = client.post("/api/count", json={"camera": 1, "t": 31.0, "direction": "in"}).json()
    assert r["summary"]["cameras"][1]["in"] == 1 and r["version"] > st["version"]
    assert client.post("/api/count", json={"camera": 1, "t": 31.0,
                                           "direction": "up"}).status_code == 400
    undone = client.post("/api/undo", json={"camera": 1}).json()
    assert undone["removed"]["t"] == 31.0 and undone["summary"]["total"]["in"] == 0
    s = client.post("/api/sensor", json={"interval": "11:30", "camera": 0, "direction": "out",
                                         "value": 2}).json()
    assert s["summary"]["intervals"][0]["cameras"][0]["sensor"] == {"out": 2}
    part = client.get("/video", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert "Manual count" in client.get("/report").text
