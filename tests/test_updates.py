"""Updating from the page: only ever a fast-forward, and nothing changed when it cannot be."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crossing_count import updates, wizard_app
from crossing_count.wizard_app import create_wizard_app

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=ENV).stdout.strip()


def commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    git(repo, "push", "-q", "origin", "HEAD:main")


def test_the_app_takes_a_newer_version_only_by_fast_forward(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    theirs, mine = tmp_path / "theirs", tmp_path / "mine"
    git(tmp_path, "clone", "-q", str(origin), str(theirs))
    git(theirs, "checkout", "-q", "-b", "main")
    commit(theirs, "app.py", "v1\n", "First version")
    git(tmp_path, "clone", "-q", str(origin), str(mine))
    started = updates.head(mine)
    assert updates.status(mine, running=started)["behind"] == 0

    commit(theirs, "app.py", "v2\n", "Smoother player")
    st = updates.status(mine, running=started)
    assert (st["behind"], st["changes"], st["restart"]) == (1, ["Smoother player"], False)
    now = updates.apply(mine)
    assert now != started and (mine / "app.py").read_text() == "v2\n"
    st = updates.status(mine, running=started)
    assert st["behind"] == 0 and st["restart"]  # the new version is here, not yet running

    commit(theirs, "app.py", "v3\n", "Another change")
    (mine / "app.py").write_text("changed here\n")  # would be overwritten: refused
    with pytest.raises(updates.UpdateError, match="nothing was changed"):
        updates.apply(mine)
    assert updates.head(mine) == now and (mine / "app.py").read_text() == "changed here\n"


def test_the_page_updates_and_restarts_the_app(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(updates, "status", lambda fetch=True: {
        "supported": True, "behind": 2, "changes": ["A", "B"], "restart": False})
    monkeypatch.setattr(updates, "apply", lambda: "abc1234")
    restarted: list[int] = []
    monkeypatch.setattr(wizard_app, "_restart", lambda: restarted.append(1))
    client = TestClient(create_wizard_app(tmp_path, tmp_path / "runs", [tmp_path]))
    assert client.get("/api/update").json()["behind"] == 2
    assert client.post("/api/update").json() == {"ok": True, "head": "abc1234"}
    for _ in range(50):
        if restarted:
            break
        time.sleep(0.05)
    assert restarted == [1]
