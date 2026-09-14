"""The count validation engine: known inputs, known answers, and honest uncertainty."""

from __future__ import annotations

import json
from pathlib import Path

from crossing_count import validation as v


def rows(pairs: list[tuple[int, int]], direction: str = "in") -> list[dict[str, object]]:
    return [{"interval": f"i{k}", "direction": direction, "truth": t, "system": s}
            for k, (t, s) in enumerate(pairs)]


def test_count_errors_are_defined_exactly() -> None:
    m = v.metrics(rows([(10, 12), (20, 15), (0, 1)]))  # errors +2, -5, +1
    assert (m["truth"], m["system"], m["error"], m["bias_pct"]) == (30, 28, -2, -6.7)
    assert (m["overcount"], m["undercount"], m["max_abs_error"]) == (3, 5, 5)
    assert (m["mae"], m["rmse"], m["wape_pct"]) == (2.67, 3.16, 26.7)
    assert (m["mape_pct"], m["mape_intervals"]) == (22.5, 2)  # the 0-crossing interval left out


def test_no_percentage_without_crossings() -> None:
    m = v.metrics(rows([(0, 3)]))
    assert m["bias_pct"] is None and m["wape_pct"] is None and m["mape_pct"] is None
    assert v.metrics([])["mae"] is None


def test_both_directions_are_added_per_interval() -> None:
    both = rows([(10, 12), (20, 18)], "in") + rows([(5, 3), (8, 10)], "out")
    m = v.by_direction(both)
    assert m["in"]["error"] == 0 and m["out"]["error"] == 0
    assert m["total"]["intervals"] == 2 and m["total"]["truth"] == 43
    assert m["total"]["wape_pct"] == 0.0  # +2 in and -2 out in the same interval cancel there
    assert m["in"]["wape_pct"] == 13.3


def test_uncertainty_needs_enough_validations_and_is_repeatable() -> None:
    few = [rows([(10, 11)]) for _ in range(4)]
    assert v.bootstrap(few, lambda s: v.metrics(s)["bias_pct"]) is None
    many = [rows([(10, 10 + k % 3)]) for k in range(8)]
    a = v.bootstrap(many, lambda s: v.metrics(s)["bias_pct"])
    assert a is not None and a == v.bootstrap(many, lambda s: v.metrics(s)["bias_pct"])
    point = v.metrics([r for c in many for r in c])["bias_pct"]
    assert a[0] <= point <= a[1]


def test_the_headline_says_what_was_measured(tmp_path: Path) -> None:
    vals = [{"id": f"v{k}", "store": f"S{k % 3}", "camera_hours": 0.25,
             "rows": [{"interval": "11:30 - 11:45", "direction": "in", "truth": 20,
                       "system": 18 + k % 2, "covered_s": 900, "cameras": 1}]} for k in range(6)]
    s = v.summary(vals)
    assert (s["validations"], s["stores"], s["crossings"]) == (6, 3, 120)
    text = v.headline(s, "RetailNext")
    assert "Across 3 stores, 6 validations" in text and "too few" in text and "95% range" in text
    assert list(s["by_traffic"]) == ["Normal traffic"]  # 20 in 15 minutes: 80 per camera-hour
    assert "no uncertainty range" in v.headline(v.summary(vals[:2]), "RetailNext")


def test_only_complete_independent_results_are_put_together(tmp_path: Path) -> None:
    good = {"fingerprint": "a" * 16, "system": "RetailNext", "status": "complete", "marked": False,
            "store": {"code": "S1"}, "cameras": ["C1"], "duration_s": 900,
            "rows": [{"interval": "whole footage", "direction": "in", "truth": 20, "system": 22,
                      "covered_s": 900, "cameras": 1}], "metrics": None}
    marked = {**good, "fingerprint": "b" * 16, "marked": True}
    other = {**good, "fingerprint": "c" * 16, "system": "Xovis"}
    for name, r in (("one", good), ("two", marked)):
        p = tmp_path / "runs" / name / "wizard" / "state.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"store": {"operator": "Alex"}, "result": r}))
    team = tmp_path / "team" / "validations" / "s1"
    team.mkdir(parents=True)
    (team / "x.json").write_text(json.dumps({"store": {"operator": "Sam"}, "result": other}))
    o = v.overview(tmp_path, tmp_path / "team")
    assert o["systems"] == ["RetailNext", "Xovis"] and o["system"] == "RetailNext"
    assert len(o["validations"]) == 1 and o["excluded"][0]["excluded"] == [
        "counted on footage showing the system's own marks"]
    assert o["summary"]["by_direction"]["in"]["bias_pct"] == 10.0
    assert v.overview(tmp_path, tmp_path / "team", "Xovis")["validations"][0]["by"] == "Sam"
