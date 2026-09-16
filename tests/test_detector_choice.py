"""Choosing the detector in the wizard: RF-DETR in the configuration that is quick, and a
plain reason when it cannot run in this copy."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crossing_count import app
from crossing_count.wizard import Wizard, WizardError, items, pipeline_commands
from crossing_count.wizard import state as wizard_state


def _commands(model: str) -> list[list[str]]:
    w = SimpleNamespace(state={"model": model, "cameras": [{"config": "c.json", "sensor": "CAM-A",
                                                           "picture": 0}]},
                        video=Path("v.mp4"), run_dir=Path("run"))
    return pipeline_commands(w)  # type: ignore[arg-type]


def test_rf_detr_runs_on_the_whole_picture_into_the_wizards_folder() -> None:
    detect = _commands(items.RFDETR)[1]
    i = detect.index("--detector")
    assert detect[i + 1] == "rfdetr" and "--naive" in detect and "--main-folder" in detect
    assert "--model" not in detect  # never the de-rotated pipeline: that takes hours
    yolo = _commands("yolo26m.pt")[1]
    assert yolo[yolo.index("--model") + 1] == "yolo26m.pt" and "--detector" not in yolo


def test_each_detection_run_knows_where_its_results_go() -> None:
    folder = app.script("detect").results_folder
    assert folder("yolo", "yolo26m.pt", "yolo26m.pt", "derotated") == "derotated"
    assert folder("yolo", "yolo11m.pt", "yolo26m.pt", "derotated") == "yolo11m-derotated"
    assert folder("rfdetr", "yolo26m.pt", "yolo26m.pt", "naive") == "rfdetr-naive"
    assert folder("rfdetr", "yolo26m.pt", "yolo26m.pt", "naive", main_folder=True) == "derotated"


def test_rf_detr_is_refused_with_the_reason_when_it_cannot_run_here(
        clean_two_tile_video: dict[str, Any], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "rfdetr" else real(name, *a))
    assert "not installed in this copy" in str(items.unavailable(items.RFDETR))
    assert items.unavailable("yolo26m.pt") is None
    w = Wizard(clean_two_tile_video["video"], clean_two_tile_video["dir"], tmp_path)
    with pytest.raises(WizardError, match="uv sync --group detectors"):
        w.set_model(items.RFDETR)
    listed = {d["model"]: d for d in w.public()["detectors"]}
    assert listed[items.RFDETR]["unavailable"] and listed["yolo26m.pt"]["unavailable"] is None
    assert "6 minutes" in str(listed[items.RFDETR]["speed"])
    monkeypatch.setattr(wizard_state, "unavailable", lambda model: None)  # installed after all
    w.set_model(items.RFDETR)
    assert w.state["model"] == items.RFDETR
    with pytest.raises(WizardError, match="unknown detector"):
        w.set_model("rfdetr-2xlarge")
