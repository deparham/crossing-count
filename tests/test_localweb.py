"""The local server answers only CrossingCount's own pages on this computer."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crossing_count import wizard_app
from crossing_count.localweb import allowed
from crossing_count.wizard_app import create_wizard_app


def test_only_this_computers_own_pages_are_answered() -> None:
    assert allowed("GET", "127.0.0.1:8780", None) and allowed("GET", "localhost:8781", None)
    assert allowed("GET", "[::1]:8780", None)
    assert allowed("POST", "127.0.0.1:8780", "http://127.0.0.1:8780")  # the page itself
    assert allowed("POST", "127.0.0.1:8780", None)  # a program, not a browser
    assert not allowed("GET", "evil.example:8780", None)  # a site pointing its name here
    assert not allowed("GET", None, None)
    assert not allowed("POST", "127.0.0.1:8780", "https://evil.example")  # another site
    assert not allowed("POST", "127.0.0.1:8780", "http://127.0.0.1:9999")  # another local server
    assert not allowed("POST", "127.0.0.1:8780", "null")  # a sandboxed or file page


def test_the_app_refuses_other_sites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stopped: list[int] = []
    monkeypatch.setattr(wizard_app, "_stop_server", lambda: stopped.append(1))
    client = TestClient(create_wizard_app(tmp_path / "sites", tmp_path, folders=[tmp_path]))
    assert client.get("/").status_code == 200
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/label/", headers={"host": "evil.example"}).status_code == 403  # mounted too
    assert client.post("/api/quit", headers={"origin": "https://evil.example"}).status_code == 403
    assert stopped == []
