"""Sharing on the network: the access code, what stays with this computer, a validation per
person, and counts that take turns."""

from __future__ import annotations

import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from crossing_count import auditlog, network, paths, wizard_app
from crossing_count.wizard.jobs import Progress, _Job
from crossing_count.wizard_app import HOST_ONLY, create_wizard_app

COLLEAGUE = ("192.168.1.20", 50123)


def share(app: Any) -> network.Access:
    """Sharing switched on, without listening on a real port: the tests' clients say where
    they are."""
    sharing = app.state.sharing
    sharing._server = SimpleNamespace(started=True, should_exit=False)
    sharing.access = network.Access()
    return sharing.access  # type: ignore[no-any-return]


@pytest.fixture
def footage(two_tile_video: dict[str, Any], tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))  # never the real settings
    folder = tmp_path / "footage"
    folder.mkdir()
    a = folder / "entrance_a.mp4"
    b = folder / "entrance_b.mp4"
    shutil.copy(two_tile_video["video"], a)
    shutil.copy(two_tile_video["video"], b)
    app = create_wizard_app(two_tile_video["dir"], tmp_path / "data", [folder])
    return {"app": app, "a": a, "b": b, "root": tmp_path / "data"}


def test_the_access_code() -> None:
    access = network.Access()
    assert len(access.code) == 9 and access.code[4] == "-"
    assert set(access.code.replace("-", "")) <= set(network.CODE_ALPHABET)
    assert access.join("WRONG-CODE") is None
    token = access.join(access.code.lower().replace("-", " "))  # as typed, or read aloud
    assert token and access.admitted(token) and not access.admitted("made-up")
    fresh = network.Access()
    for k in range(network.MAX_FAILURES):
        assert fresh.join("AAAA-AAAA", now=100.0 + k) is None
    assert fresh.locked_out(now=120.0) and fresh.join(fresh.code, now=120.0) is None
    assert fresh.join(fresh.code, now=120.0 + network.FAIL_WINDOW_S) is not None  # it passes


def test_nobody_on_the_network_gets_in_while_sharing_is_off(footage: dict[str, Any]) -> None:
    here = TestClient(footage["app"])
    there = TestClient(footage["app"], client=COLLEAGUE)
    assert here.get("/").status_code == 200
    assert there.get("/").status_code == 403
    assert there.get("/join").status_code == 403


def test_people_on_the_network_need_the_code(footage: dict[str, Any]) -> None:
    access = share(footage["app"])
    there = TestClient(footage["app"], client=COLLEAGUE, follow_redirects=False)
    page = there.get("/")
    assert page.status_code == 401 and "access code" in page.text
    assert there.get("/api/state").status_code == 401
    assert there.get("/runs/").status_code == 401 and there.get("/label/").status_code == 401
    wrong = there.post("/join", data={"code": "AAAA-AAAA", "next": "/"})
    assert wrong.status_code == 401 and "not right" in wrong.text
    via_link = there.get(f"/runs/?code={access.code}")
    assert via_link.status_code == 303 and via_link.headers["location"] == "/runs/"
    assert network.COOKIE in via_link.headers["set-cookie"]
    assert "httponly" in via_link.headers["set-cookie"].lower()
    assert there.get("/").status_code == 200 and there.get("/api/videos").status_code == 200
    assert there.get("/api/network").json() == {"on_host": False, "on": True}
    # the code is never shown to someone on the network, nor the join page open elsewhere
    assert access.code not in there.get("/").text
    stranger = TestClient(footage["app"], client=("192.168.1.99", 5000), follow_redirects=False)
    evil = stranger.post("/join", data={"code": access.code, "next": "//evil.example/"})
    assert evil.status_code == 303 and evil.headers["location"] == "/"  # never off-site


def test_what_stays_with_this_computer(footage: dict[str, Any],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_quit() -> None:
        raise AssertionError("quit from the network")

    monkeypatch.setattr(wizard_app, "_stop_server", must_not_quit)
    access = share(footage["app"])
    there = TestClient(footage["app"], client=COLLEAGUE)
    there.get(f"/?code={access.code}")
    routes = {(m, r.path) for r in footage["app"].routes if isinstance(r, Route)
              for m in (r.methods or ())}
    assert HOST_ONLY <= routes  # each is a real route: a renamed one would lose its guard
    for method, path in sorted(HOST_ONLY):
        got = there.request(method, path, json={})
        assert got.status_code == 403 and "Only on the computer running" in got.text, path
    # footage from the footage folders only, not anywhere on this computer's disk
    assert there.get("/api/videos", params={"folder": "/"}).status_code == 403
    assert there.post("/api/open", json={"path": str(Path(sys.executable))}).status_code == 403
    assert there.post("/label/api/add", json={"path": str(Path(sys.executable))}).status_code == 403
    # a page on another site cannot use the cookie to change anything
    forged = there.post("/api/mode", json={"mode": "manual"},
                        headers={"Origin": "http://evil.example"})
    assert forged.status_code == 403
    # on this computer its own network name resolves to 127.0.0.1: that name works there too,
    # while shared, and no other name does
    footage["app"].state.sharing.urls = ["http://Front-Desk-Mac.local:8781/"]
    local = TestClient(footage["app"], base_url="http://front-desk-mac.local:8781")
    assert local.get("/api/network").status_code == 200
    rebound = TestClient(footage["app"], base_url="http://attacker.example:8781")
    assert rebound.get("/api/network").status_code == 403
    # this computer is unaffected
    here = TestClient(footage["app"])
    shown = here.get("/api/network").json()
    assert shown["on_host"] is True and shown["code"] == access.code  # the code is shown here
    assert here.post("/api/settings", json={"examples_dir": ""}).status_code == 200


def test_each_person_works_on_their_own_validation(footage: dict[str, Any],
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    access = share(footage["app"])
    here = TestClient(footage["app"])
    there = TestClient(footage["app"], client=COLLEAGUE)
    there.get(f"/?code={access.code}")
    assert here.post("/api/open", json={"path": str(footage["a"])}).status_code == 200
    assert there.get("/api/state").status_code == 409  # nothing chosen yet: not this computer's
    taken = there.post("/api/open", json={"path": str(footage["a"])})
    assert taken.status_code == 409 and "this computer" in taken.json()["detail"]
    mine = there.post("/api/open", json={"path": str(footage["b"])})
    assert mine.status_code == 200 and mine.json()["on_host"] is False
    assert here.post("/api/direction", json={"direction": "in"}).status_code == 200
    assert here.get("/api/state").json()["filename"] == "entrance_a.mp4"
    theirs = there.get("/api/state").json()
    assert theirs["filename"] == "entrance_b.mp4" and theirs["direction"] is None
    assert there.post("/api/store", json={"operator": "Sam"}).status_code == 200
    (person,) = here.get("/api/network").json()["people"]
    assert person["who"] == f"Sam ({COLLEAGUE[0]})" and person["footage"] == "entrance_b.mp4"
    assert person["counting"] is False and 0 <= person["idle_s"] <= 5
    assert paths.load_settings().get("operator") != "Sam"  # theirs, not this computer's setting
    # the drawing pages are each person's own
    assert here.get("/api/state").json()["draw_url"] != there.get("/api/state").json()["draw_url"]
    # footage left alone for long enough (and not counting) can be opened by someone else
    monkeypatch.setattr(wizard_app, "LOCK_IDLE_S", 0)
    assert there.post("/api/open", json={"path": str(footage["a"])}).status_code == 200
    assert here.get("/api/state").status_code == 409  # this computer's was closed, and saved
    actions = [e for e in auditlog.recent(50, footage["root"]) if e.get("from")]
    assert actions and all(e["from"] == COLLEAGUE[0] for e in actions)


def test_counts_take_turns_on_this_computer() -> None:
    finished: list[tuple[str, float]] = []
    done = threading.Event()

    def finish(name: str) -> Any:
        def call(status: str, error: str | None, log: list[str]) -> None:
            finished.append((f"{name}:{status}", time.monotonic()))
            if len(finished) == 3:
                done.set()
        return call

    sleep = [[sys.executable, "-c", "import time; time.sleep(2.5)"]]
    first = _Job(sleep, Progress(), finish("first"))
    time.sleep(0.3)
    second_progress = Progress()
    second = _Job(sleep, second_progress, finish("second"))
    third = _Job(sleep, Progress(), finish("third"))
    deadline = time.monotonic() + 2.0  # the first holds the computer for 2.5 s at least
    while second_progress.stage != "queued" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert second_progress.stage == "queued" and "another count" in second_progress.message
    third.stop()  # stopped while waiting: never starts
    assert done.wait(20), finished
    order = [name for name, _ in finished]
    assert order[0] == "third:stopped" and order[1:] == ["first:done", "second:done"]
    assert first.running is False and second.running is False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_sharing_starts_and_stops(footage: dict[str, Any]) -> None:
    port = _free_port()
    sharing = network.Sharing(footage["app"], port, bind="127.0.0.1")  # no firewall prompt here
    assert sharing.status() == {"on": False, "port": port, "error": None}
    assert sharing.start()
    try:
        st = sharing.status()
        assert st["on"] and st["code"] and st["links"][0].endswith(f"?code={st['code']}")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert r.status == 200 and b"Count wizard" in r.read()
        first = st["code"]
    finally:
        sharing.stop()
    assert not sharing.on
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
    assert sharing.start()  # at once again, on the port just closed (Linux keeps it a while)
    try:
        assert sharing.status()["code"] != first  # a new code each time
    finally:
        sharing.stop()
    other = network.Sharing(footage["app"], _free_port(), bind="127.0.0.1")
    with socket.socket() as busy:  # another program listening there
        busy.bind(("127.0.0.1", other.port))
        busy.listen()
        assert not other.start() and "in use" in str(other.error)
