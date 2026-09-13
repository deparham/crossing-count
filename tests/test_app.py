"""The installed program: where files live, and one executable running every step."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from crossing_count import app, paths
from crossing_count import wizard as wz


def test_data_folder_can_be_moved_and_is_prepared(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    root = paths.prepare_data_root()
    assert root == tmp_path / "home" and (root / "sites").is_dir() and (root / "runs").is_dir()
    assert paths.sites_dir() == root / "sites"
    assert paths.models_dirs()[0] == root / "models"
    assert paths.save_settings({"examples_dir": "/shared"})["examples_dir"] == "/shared"
    assert json.loads((root / "settings.json").read_text())["examples_dir"] == "/shared"
    assert paths.load_settings()["examples_dir"] == "/shared"


def test_an_installed_copy_starts_with_the_shipped_drawings(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "sites").mkdir(parents=True)
    (bundle / "sites" / "cam.json").write_text("{}")
    monkeypatch.setattr(paths, "bundle_root", lambda: bundle)
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "sites").mkdir(parents=True)
    (tmp_path / "home" / "sites" / "mine.json").write_text('{"kept": true}')
    paths.prepare_data_root()
    assert (tmp_path / "home" / "sites" / "cam.json").exists()
    assert json.loads((tmp_path / "home" / "sites" / "mine.json").read_text()) == {"kept": True}


def test_every_step_is_a_sub_command(capsys: pytest.CaptureFixture[str],
                                     tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    for name in ("gate", "detect", "count"):
        with pytest.raises(SystemExit) as done:
            app.main([name, "--help"])
        assert done.value.code == 0
    out = capsys.readouterr().out
    assert "motion gating" in out and "candidate crossings" in out and "Manual count" in out


def test_the_wizard_starts_steps_as_scripts_or_as_sub_commands(
        monkeypatch: pytest.MonkeyPatch) -> None:
    assert wz.tool("gate") == [sys.executable, str(paths.SOURCE_ROOT / "gate.py")]
    monkeypatch.setattr(paths, "FROZEN", True)
    assert wz.tool("detect") == [sys.executable, "detect"]
