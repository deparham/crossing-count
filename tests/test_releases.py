"""The installed app hears of GitHub's newer builds and updates itself; built-in brands."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import time
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any, ClassVar

import keyring
import pytest
from fastapi.testclient import TestClient

from crossing_count import builtin, paths, releases
from crossing_count import retailnext as rn
from crossing_count.wizard_app import create_wizard_app

ASSET = {"id": 1, "name": "CrossingCount-0.1.12.dmg", "size": 5}
RELEASES: list[dict[str, Any]] = [
    {"tag_name": "mac-b12", "assets": [ASSET], "target_commitish": "bbb", "body": "Newer"},
    {"tag_name": "mac-b9", "assets": [ASSET]},
    {"tag_name": "win-b40", "assets": [ASSET]},
    {"tag_name": "mac-b15", "assets": []},  # its file still uploading
    {"tag_name": "mac-b14", "draft": True, "assets": [ASSET]},
]


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    info = {"platform": "mac", "build": 10, "commit": "aaa", "repo": "o/r"}
    monkeypatch.setattr(builtin, "build", lambda: info)
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    return info


def test_the_newest_build_of_this_platform_that_has_its_file() -> None:
    assert releases.newest(RELEASES, "mac", 10)["build"] == 12  # type: ignore[index]
    assert releases.newest(RELEASES, "mac", 12) is None
    assert releases.newest(RELEASES, "win", 1)["build"] == 40  # type: ignore[index]


def test_status_asks_for_a_token_then_names_the_newer_build(built: dict[str, Any],
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(releases, "token", lambda: None)
    s = releases.status()
    assert s["needs_token"] and not s["available"]
    monkeypatch.setattr(releases, "token", lambda: "t")
    asked: list[str] = []

    def fake_get(path: str, tok: str) -> Any:
        asked.append(path)
        if "/compare/" in path:
            return {"commits": [{"commit": {"message": "Older change\n\nmore"}},
                                {"commit": {"message": "Newest change"}}]}
        return RELEASES

    monkeypatch.setattr(releases, "_get", fake_get)
    s = releases.status()
    assert s["available"] and (s["build"], s["latest"], s["behind"]) == (10, 12, 2)
    assert s["changes"] == ["Newest change", "Older change"]
    assert "/repos/o/r/compare/aaa...bbb" in asked


def test_a_copy_not_built_by_github_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtin, "build", lambda: None)
    s = releases.status()
    assert not s["supported"] and "not built by GitHub" in s["reason"]


class _Resp(io.BytesIO):
    headers: ClassVar[dict[str, str]] = {"Content-Length": "5"}


def test_the_download_follows_githubs_link_without_the_token(
        built: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(releases, "token", lambda: "the-token")
    monkeypatch.setattr(releases, "_get", lambda path, tok: RELEASES)
    seen: dict[str, Any] = {}

    def fake_open(req: Any, timeout: float, context: Any) -> Any:
        seen["api"] = req.get_header("Authorization")
        headers = Message()
        headers["Location"] = "https://objects.example/file"
        raise urllib.error.HTTPError(req.full_url, 302, "Found", headers, None)

    def fake_download(req: Any, timeout: float, context: Any) -> Any:
        seen["link"] = req.get_header("Authorization")
        return _Resp(b"12345")

    monkeypatch.setattr(releases, "_open", fake_open)
    monkeypatch.setattr(releases, "_download_open", fake_download)
    got = releases.download()
    assert got.read_bytes() == b"12345" and got.name == "CrossingCount-0.1.12.dmg"
    assert seen == {"api": "Bearer the-token", "link": None}


def test_the_mac_app_is_swapped_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "Applications" / "CrossingCount.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "v").write_text("old")

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["hdiutil", "attach"]:
            inside = Path(cmd[cmd.index("-mountpoint") + 1]) / "CrossingCount.app" / "Contents"
            inside.mkdir(parents=True)
            (inside / "v").write_text("new")
        elif cmd[0] == "ditto":
            shutil.copytree(cmd[1], cmd[2])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(releases, "_run", fake_run)
    releases.install_mac(tmp_path / "update.dmg", app)
    assert (app / "Contents" / "v").read_text() == "new"
    assert [p.name for p in app.parent.iterdir()] == ["CrossingCount.app"]


def test_windows_installs_silently_where_it_was_installed() -> None:
    local = r"C:\Users\a\AppData\Local"
    mine = releases.installer_args(Path("setup.exe"), Path(local + r"\Programs\Crossing Count"), local)
    assert mine[1:] == ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                        "/CURRENTUSER"]
    everyone = releases.installer_args(Path("setup.exe"), Path(r"C:\Program Files\Crossing Count"),
                                       local)
    assert everyone[-1] == "/ALLUSERS"


def test_the_installed_app_updates_in_the_background(built: dict[str, Any], tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(releases, "status", lambda: {"supported": True, "available": True,
                                                     "installed": True, "behind": 2})

    def fake_update(job: dict[str, Any], stop: Any) -> None:
        job.update(state="restarting")

    monkeypatch.setattr(releases, "run_update", fake_update)
    client = TestClient(create_wizard_app(tmp_path / "sites", tmp_path, folders=[tmp_path]))
    assert client.get("/api/update").json()["available"]
    assert client.get("/api/update?fetch=false").json()["build"] == 10  # no GitHub asked
    assert client.post("/api/update").json() == {"ok": True, "job": True}
    for _ in range(50):
        if client.get("/api/update/job").json().get("state") == "restarting":
            break
        time.sleep(0.02)
    assert client.get("/api/update/job").json()["state"] == "restarting"


def test_brands_built_into_the_app_are_usable_but_not_removable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    monkeypatch.setattr(builtin, "_data", lambda: {"retailnext": {"rag": {"access_key": "a",
                                                                          "secret_key": "s"}}})
    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    assert rn.subscriptions() == ["rag"] and rn.saved_subscriptions() == []
    conn = rn.load_connection("rag")
    assert conn is not None and (conn.access_key, conn.secret_key) == ("a", "s")
    client = TestClient(create_wizard_app(tmp_path / "sites", tmp_path, folders=[tmp_path]))
    r = client.post("/api/retailnext/forget", json={"subscription": "rag"})
    assert r.status_code == 400 and "built into this app" in r.json()["detail"]
    assert client.get("/api/retailnext").json()["builtin"] == ["rag"]


def test_the_build_writes_its_number_and_what_it_carries(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    spec = importlib.util.spec_from_file_location(
        "build_info", paths.SOURCE_ROOT / "packaging" / "build_info.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for k, v in {"GITHUB_RUN_NUMBER": "57", "GITHUB_SHA": "abc", "GITHUB_REPOSITORY": "o/r",
                 "RETAILNEXT_BRANDS": json.dumps({"rag": {"access_key": "a", "secret_key": "s"}})
                 }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("UPDATE_TOKEN", raising=False)
    assert mod.main(["mac"], pkg=tmp_path) == 0
    assert "brands built in: rag" in capsys.readouterr().out  # the names, never a key
    assert json.loads((tmp_path / "build.json").read_text())["build"] == 57
    monkeypatch.setattr(builtin, "FILE", tmp_path / "builtin.dat")
    builtin._data.cache_clear()
    try:
        assert builtin.retailnext() == {"rag": {"access_key": "a", "secret_key": "s"}}
        assert builtin.github_token() is None
    finally:
        builtin._data.cache_clear()
