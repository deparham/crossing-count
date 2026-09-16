"""State files: a bad one says which file and what is wrong, and a count is never lost."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from crossing_count.wizard import Wizard, WizardError, schema

GOOD: dict[str, Any] = {"schema": "wizard/1", "fingerprint": "abc123", "filename": "x.mp4",
                        "duration_s": 60.0}


def _write(tmp_path: Path, data: Any) -> Path:
    p = tmp_path / "state.json"
    p.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return p


def test_what_a_later_version_added_is_filled_in(tmp_path: Path) -> None:
    state, filled = schema.load_state(_write(tmp_path, GOOD), "abc123")
    assert state["job"]["status"] == "idle" and state["manual"]["counts"] == []
    assert state["store"]["operator"] == "" and state["rules"]["children"] == "count"
    assert {"job", "manual", "store", "sampling", "marks_detected"} <= set(filled)
    # a second read fills in nothing: the file is already complete
    again, none_filled = schema.load_state(_write(tmp_path, state), "abc123")
    assert none_filled == [] and again["schema"] == schema.SCHEMA


def test_nothing_is_overwritten_and_nothing_unknown_is_dropped(tmp_path: Path) -> None:
    kept = {**GOOD, "model": "yolo11m.pt", "decisions": [{"action": "answer"}],
            "something_a_newer_version_wrote": {"keep": True},
            "manual": {"counts": [{"t": 1.0}]}}
    state, filled = schema.load_state(_write(tmp_path, kept), "abc123")
    assert state["model"] == "yolo11m.pt" and state["decisions"] == [{"action": "answer"}]
    assert state["something_a_newer_version_wrote"] == {"keep": True}
    assert state["manual"]["counts"] == [{"t": 1.0}]  # the count itself is untouched
    assert state["manual"]["next_id"] == 1 and "manual.next_id" in filled  # the rest filled in


def test_a_file_that_cannot_be_read_says_which_and_why(tmp_path: Path) -> None:
    with pytest.raises(schema.StateError, match="not readable as JSON"):
        schema.load_state(_write(tmp_path, "{oops"), "abc123")
    with pytest.raises(schema.StateError, match="Nothing in it has been changed"):
        schema.load_state(_write(tmp_path, "{oops"), "abc123")
    assert (tmp_path / "state.json").read_text() == "{oops"  # left exactly as it was
    with pytest.raises(schema.StateError, match="contents are a list"):
        schema.load_state(_write(tmp_path, [1, 2]), "abc123")
    with pytest.raises(schema.StateError, match="missing fingerprint"):
        schema.load_state(_write(tmp_path, {"filename": "x.mp4", "duration_s": 1.0}), "abc123")
    with pytest.raises(schema.StateError, match="different video with the same name"):
        schema.load_state(_write(tmp_path, GOOD), "another-fingerprint")
    with pytest.raises(schema.StateError, match="could not be read"):
        schema.load_state(tmp_path / "gone.json", "abc123")


def test_a_newer_schema_is_refused_rather_than_half_understood(tmp_path: Path) -> None:
    with pytest.raises(schema.StateError, match="newer version of CrossingCount"):
        schema.load_state(_write(tmp_path, {**GOOD, "schema": "wizard/9"}), "abc123")
    with pytest.raises(schema.StateError, match="not a count's state"):
        schema.load_state(_write(tmp_path, {**GOOD, "schema": "gold/1"}), "abc123")


def test_the_wizard_opens_an_older_file_and_refuses_a_broken_one(
        clean_two_tile_video: dict[str, Any], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    w = Wizard(clean_two_tile_video["video"], clean_two_tile_video["dir"], tmp_path)
    # an older file: no job, no manual, no rules, and a key this version does not know
    old = {k: w.state[k] for k in ("schema", "video", "filename", "fingerprint", "duration_s",
                                   "clock_start", "clock_end", "tz", "created_at")}
    old["from_an_older_build"] = 7
    w.path.write_text(json.dumps(old), encoding="utf-8")
    again = Wizard(clean_two_tile_video["video"], clean_two_tile_video["dir"], tmp_path)
    assert again.state["job"]["status"] == "idle" and again.state["from_an_older_build"] == 7
    assert "job" in again.filled_in and again.state["model"] in ("yolo26m.pt",)
    w.path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(WizardError, match="not readable as JSON"):
        Wizard(clean_two_tile_video["video"], clean_two_tile_video["dir"], tmp_path)
