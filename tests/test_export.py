"""M4 export: the CSV layout, the metrics, the report and the sensor comparison."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from crossing_count.export import (
    CSV_COLUMNS,
    SensorCounts,
    build_export,
    csv_text,
    parse_sensor_arg,
    report_html,
    write_export,
)
from crossing_count.review import ReviewSession

START = datetime(2026, 9, 12, 11, 30, 0)  # noqa: DTZ001 - wall clock as in the filename


def reviewed(run: Path) -> ReviewSession:
    s = ReviewSession(run, "Pat")
    cands = [i for i in s.items if i["kind"] == "candidate"]
    s.decide(cands[0]["id"], "accept", tags=["child"])
    s.decide(cands[1]["id"], "split")
    s.decide(cands[2]["id"], "reject")
    unex = next(i["id"] for i in s.items if i["kind"] == "unexplained")
    s.decide(unex, "scanned", added=[{"t": 3.0, "direction": "out"}])
    return s


def test_csv_has_the_header_block_then_the_manual_tools_columns(fresh: Path) -> None:
    export = build_export(reviewed(fresh), START)
    cam = next(c for c in export["cameras"] if c["sensor"] == "CAM-A")
    rows = list(csv.reader(io.StringIO(csv_text(export, cam))))
    header = {r[0]: r[1] for r in rows[: rows.index([])] if len(r) >= 2}
    assert header["sensor"] == "CAM-A" and header["operator"] == "Pat"
    assert header["video_start_clock"] == "2026-09-12T11:30:00"
    assert header["rule"] == "line+mask" and "mask_zone" in header
    assert "review_incomplete" in header
    table = rows[rows.index([]) + 1:]
    assert table[0] == CSV_COLUMNS
    assert [r[3] for r in table[1:]] == ["out", "in", "in", "in"]  # sorted by time
    first_in = next(r for r in table[1:] if r[3] == "in")
    assert first_in[2] == "11:30:21" and first_in[4] == "child"


def test_totals_metrics_and_sensor_accuracy(fresh: Path, tmp_path: Path) -> None:
    sensor = SensorCounts(total={"in": 4, "out": 2}, per_camera={"CAM-A": {"in": 3}})
    export = build_export(reviewed(fresh), START, sensor)
    assert export["total"] == {"in": 3, "out": 1}
    assert export["total_comparison"]["in"]["accuracy_pct"] == pytest.approx(66.7, abs=0.1)
    assert export["total_comparison"]["out"]["error"] == 1
    a = next(c for c in export["cameras"] if c["sensor"] == "CAM-A")
    assert a["metrics"]["splits"] == 1 and a["metrics"]["accepted"] == 1
    assert a["metrics"]["proposal_precision"] == 1.0
    assert export["warnings"] and "not finished" in export["warnings"][0]
    assert list(export["intervals"]) == ["11:30"]
    paths = write_export(export, tmp_path)
    assert {p.name for p in paths} == {"CAM-A.csv", "CAM-B.csv", "pipeline_metrics.json",
                                       "report.html"}
    metrics = json.loads((tmp_path / "pipeline_metrics.json").read_text())
    assert metrics["cameras"]["CAM-B"]["rejected"] == 1
    report = (tmp_path / "report.html").read_text()
    assert "Verified counts" in report and "http" not in report.split("<main>")[0]


def test_zero_against_zero_is_full_accuracy_and_totals_add_up(fresh: Path) -> None:
    sensor = SensorCounts(per_camera={"CAM-A": {"in": 0, "out": 0}, "CAM-B": {"in": 0, "out": 0}})
    export = build_export(ReviewSession(fresh), START, sensor)  # nothing reviewed: 0 verified
    assert export["total_comparison"]["in"] == {"sensor": 0, "verified": 0, "error": 0,
                                                "error_pct": None, "accuracy_pct": 100.0}
    report = report_html(export)
    assert "<td class=n>0</td><td class=n>0</td></tr>" in report  # zeros, not dashes
    over = build_export(ReviewSession(fresh), START,
                        SensorCounts(total={"in": 3, "out": 0}))["total_comparison"]["in"]
    assert over["accuracy_pct"] is None and over["error"] == 3


def test_sensor_argument_parsing() -> None:
    assert parse_sensor_arg("in=20,out=23") == (None, {"in": 20, "out": 23})
    assert parse_sensor_arg("CN-123-PB1:in=12") == ("CN-123-PB1", {"in": 12})
    with pytest.raises(ValueError):
        parse_sensor_arg("CN-1:sideways=3")
