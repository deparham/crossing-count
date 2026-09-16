"""The installed program: where files live, and one executable running every step."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from crossing_count import app, paths, updates, window
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
    for name in ("gate", "detect"):
        with pytest.raises(SystemExit) as done:
            app.main([name, "--help"])
        assert done.value.code == 0
    out = capsys.readouterr().out
    assert "motion gating" in out and "candidate crossings" in out
    # the standalone hand counter, reviewer and exporter are gone: the wizard does all
    # three, and their names are no longer sub-commands
    assert not {"count", "review", "export"} & set(app.COMMANDS)


def test_the_wizard_starts_steps_as_scripts_or_as_sub_commands(
        monkeypatch: pytest.MonkeyPatch) -> None:
    assert wz.tool("gate") == [sys.executable, str(paths.SOURCE_ROOT / "gate.py")]
    monkeypatch.setattr(paths, "FROZEN", True)
    assert wz.tool("detect") == [sys.executable, "detect"]


def test_opening_it_again_finds_the_one_already_running() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert app.serving(port)
    assert not app.serving(port)


def test_a_restart_brings_the_window_back_and_opens_no_second_tab(
        monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []

    class Replaced(Exception):
        """os.execv does not come back."""

    def fake_execv(exe: str, args: list[str]) -> None:
        started.append(list(args))
        raise Replaced

    monkeypatch.setattr(updates, "_uv", lambda: "/uv")
    monkeypatch.setattr(os, "execv", fake_execv)
    with pytest.raises(Replaced):
        updates.restart(["wizard.py", "--port", "8780"])  # its own window: started as it was
    with pytest.raises(Replaced):
        updates.restart(["wizard.py", "--browser"])  # the page in the browser reloads itself
    assert started[0][-2:] == ["--port", "8780"] and "--no-browser" not in started[0]
    assert started[1][-2:] == ["--browser", "--no-browser"]


def test_notices_to_the_mac_are_quoted_safely() -> None:
    assert window.applescript_text('Build "57"\nis ready \\o/') == '"Build \\"57\\" is ready \\\\o/"'
    assert window.available()
