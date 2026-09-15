"""Counting by hand: corrections with a reason, uncertain marks, notes, and settling two
people's disagreements."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from crossing_count import auditlog, gold
from crossing_count.webapp import Setup
from crossing_count.wizard import Wizard, WizardError

STORE = next(c for c in (f"A-{i}" for i in range(500)) if gold.split_of(c) == "train")


@pytest.fixture
def counter(clean_two_tile_video: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wizard:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    w = Wizard(clean_two_tile_video["video"], clean_two_tile_video["dir"], tmp_path)
    w.set_mode("manual")
    w.set_marks(True)
    s = Setup(w.video, clean_two_tile_video["dir"])
    w.set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                        [{"picture": 0, "name": "CN-9-PB1", "include": True},
                         {"picture": 1, "name": "CN-9-R2", "include": False}])
    w.state["marks"] = False
    w.set_direction("both")
    w.state["clock_start"] = "2026-09-12T11:30:00"
    w.set_store(code=STORE, operator="Alex")
    return w


def test_a_count_is_corrected_with_what_it_was_and_why(counter: Wizard, tmp_path: Path) -> None:
    assert counter.public()["fps"] > 0  # for stepping one frame at a time
    c = counter.manual_add("CN-9-PB1", 1.0, "in", note="pushing a pram")
    assert c["note"] == "pushing a pram"
    edited = counter.manual_edit(c["id"], t=1.4, direction="out", reason="went out, not in")
    assert (edited["t"], edited["direction"]) == (1.4, "out") and "edited_at" in edited
    last = counter.state["decisions"][-1]
    assert last["action"] == "hand_count_edited" and last["reason"] == "went out, not in"
    assert (last["before"]["direction"], last["after"]["direction"]) == ("in", "out")
    entry = auditlog.recent(1, tmp_path)[0]
    assert entry["action"] == "hand_count_edited" and entry["before"]["t"] == 1.0
    assert entry["reason"] == "went out, not in"
    with pytest.raises(WizardError, match="outside the video"):
        counter.manual_edit(c["id"], t=10_000.0)
    with pytest.raises(WizardError, match="no such count"):
        counter.manual_edit(999, direction="in")


def test_an_uncertain_crossing_is_listed_but_never_counted(counter: Wizard) -> None:
    counter.manual_add("CN-9-PB1", 1.0, "in")
    u = counter.manual_add("CN-9-PB1", 2.0, "in", uncertain=True, note="maybe a reflection")
    c = counter.counts()
    assert (c["verified"]["in"], c["unsure"]["in"]) == (1, 1)
    assert [r["t"] for r in counter.verified_rows()] == [1.0]
    (row,) = counter.unsure_rows()
    assert row["t"] == 2.0 and "maybe a reflection" in row["found"]
    cam = counter.manual_summary()["cameras"][0]
    assert (cam["in"], cam["uncertain"]) == (1, 1)
    counter.manual_edit(u["id"], uncertain=False, reason="looked again: a person")
    assert counter.counts()["verified"]["in"] == 2


def _full(w: Wizard, marks: list[tuple[float, str, bool]]) -> None:
    dur = float(w.state["duration_s"])
    for frac, d, unsure in marks:
        w.manual_add("CN-9-PB1", round(dur * frac, 2), d, uncertain=unsure)
    w.manual_watched("CN-9-PB1", 0.0, dur)
    w.manual_done()


def test_disagreements_are_settled_with_a_reason_and_both_counts_kept(
        counter: Wizard, tmp_path: Path) -> None:
    _full(counter, [(0.1, "in", False), (0.5, "out", False), (0.9, "in", True)])
    gold.save(counter.state, counter.run_dir, [], "", tmp_path)
    counter.recount()
    counter.set_store(operator="Sam")
    _full(counter, [(0.1, "in", False)])  # Sam saw neither the OUT nor the doubtful IN
    clip = gold.save(counter.state, counter.run_dir, [], "", tmp_path)
    a = clip["agreement"]
    assert a["status"] == "disagreement (unresolved)" and len(a["disputes"]) == 2
    out, mark = a["disputes"]
    assert out["what"].startswith("only Alex counted OUT") and "uncertain" in mark["what"]
    assert (clip["crossings"], clip["uncertain"]) == (1, 2)
    with pytest.raises(gold.GoldError, match="reason"):
        gold.adjudicate(counter.state, [{"camera": out["camera"], "t": out["t"],
                                         "decision": "out", "reason": " "}], "Pat", tmp_path)
    settled = gold.adjudicate(counter.state, [{"camera": out["camera"], "t": out["t"],
                                               "decision": "out", "reason": "clearly walks out"}],
                              "Pat", tmp_path)
    assert settled["agreement"]["status"] == "partly settled"
    assert (settled["crossings"], settled["uncertain"]) == (2, 1)  # the OUT is now truth
    done = gold.adjudicate(counter.state, [{"camera": mark["camera"], "t": mark["t"],
                                            "decision": "none", "reason": "a reflection"}],
                           "Pat", tmp_path)
    assert done["agreement"]["status"] == "settled" and (done["crossings"], done["uncertain"]) == (2, 0)
    rec = gold.find(counter.state, tmp_path)
    assert rec is not None and len(rec["reviews"]) == 2  # both counts kept as they were
    assert [h["decision"] for h in rec["adjudication"]["history"]] == ["out", "none"]
    assert {e["action"] for e in auditlog.recent(50, tmp_path)} >= {"adjudicated"}
    with pytest.raises(gold.GoldError, match="no disagreement"):
        gold.adjudicate(counter.state, [{"camera": "CN-9-PB1", "t": 0.0, "decision": "in",
                                         "reason": "x"}], "Pat", tmp_path)
