"""Keys in the Keychain: the installed app and the project folder each keep their own entries."""

from __future__ import annotations

import json
from pathlib import Path

import keyring
import keyring.errors
import pytest
from fastapi.testclient import TestClient

from crossing_count import paths
from crossing_count import retailnext as rn
from crossing_count.wizard_app import create_wizard_app


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[tuple[str, str], str]:
    """A Keychain like the Mac's: an entry made by one copy cannot be replaced by the other."""
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    store = {(rn.SERVICE, "rag"): json.dumps({"access_key": "A", "secret_key": "S"})}
    owner = {(rn.SERVICE, "rag"): "project"}

    def me() -> str:
        return "app" if paths.FROZEN else "project"

    def set_password(service: str, name: str, value: str) -> None:
        if owner.get((service, name), me()) != me():
            raise keyring.errors.PasswordSetError("Can't store password on keychain: (-25244, 'Unknown Error')")
        store[(service, name)] = value
        owner[(service, name)] = me()

    monkeypatch.setattr(keyring, "set_password", set_password)
    monkeypatch.setattr(keyring, "get_password", lambda service, name: store.get((service, name)))
    return store


def test_the_installed_app_reads_the_project_key_and_writes_its_own(
        keychain: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "FROZEN", True)
    conn = rn.reconnect("rag")  # saved by the project folder's copy: nothing to type
    assert conn is not None and conn.access_key == "A" and rn.subscriptions() == ["rag"]
    rn.save_connection("rag", "A2", "S2")  # a new key goes to the app's own entry: no clash
    assert json.loads(keychain[(rn.APP_SERVICE, "rag")])["access_key"] == "A2"
    assert rn.load_connection("rag").access_key == "A2"  # type: ignore[union-attr]


def test_a_keychain_refusal_is_said_plainly(keychain: dict[tuple[str, str], str],
                                            monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "FROZEN", True)
    rn.save_connection("rag", "A2", "S2")  # the app now owns its entry
    monkeypatch.setattr(paths, "FROZEN", False)  # the project folder may not replace it
    monkeypatch.setattr(rn, "SERVICE", rn.APP_SERVICE)
    with pytest.raises(rn.RetailNextError, match="Keychain would not keep the key"):
        rn.save_connection("rag", "A3", "S3")


def test_the_page_connects_a_brand_whose_key_is_already_here(
        keychain: dict[tuple[str, str], str], tmp_path: Path) -> None:
    client = TestClient(create_wizard_app(tmp_path / "sites", tmp_path, folders=[tmp_path]))
    r = client.post("/api/retailnext/reconnect", json={"subscription": "rag"}).json()
    assert r["connected"] and r["subscriptions"] == ["rag"]
    assert client.post("/api/retailnext/reconnect", json={"subscription": "gazman"}).json()[
        "connected"] is False
