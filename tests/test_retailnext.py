"""RetailNext's API, against a fake server: no test reaches the network or a real key."""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Self

import keyring
import pytest

from crossing_count import retailnext as rn


class Reply:
    def __init__(self, body: Any) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def http_error(code: int, text: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", None, io.BytesIO(text.encode()))  # type: ignore[arg-type]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Answers queued by a test; each request is kept for checking."""
    answers: list[Any] = []
    seen: list[urllib.request.Request] = []

    def fake_open(req: urllib.request.Request, timeout: float, context: Any) -> Reply:
        seen.append(req)
        a = answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return Reply(a)

    monkeypatch.setattr(rn, "_open", fake_open)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    answers.append(seen)  # the first item is the list of requests seen
    return answers


CONN = rn.Connection("acme", "AK", "SK")


def test_subscription_names() -> None:
    assert rn.subscription_name("https://Acme.api.retailnext.net/v2/") == "acme"
    assert rn.subscription_name("rag.cloud.retailnext.net") == "rag"  # the web address
    assert rn.subscription_name("acme-au") == "acme-au"
    with pytest.raises(rn.RetailNextError):
        rn.subscription_name("not a name!")


def test_a_query_is_a_basic_auth_post(server: list[Any]) -> None:
    seen = server.pop(0)
    server.append({"nodes": [[{"uuid": "u1", "location_type": "store", "name": "A"}],
                             [{"uuid": "u2", "location_type": "zone", "name": "B"}]]})
    nodes = rn.locations(CONN, ["store"])
    assert [n["uuid"] for n in nodes] == ["u1", "u2"]
    (req,) = seen
    assert req.full_url == "https://acme.api.retailnext.net/v2/location" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Basic " + base64.b64encode(b"AK:SK").decode()
    assert json.loads(req.data)["where"] == {"and": [{"location_types": ["store"]}]}


def test_retailnexts_hiccups_are_retried_but_a_refused_key_is_not(server: list[Any]) -> None:
    seen = server.pop(0)
    server.extend([http_error(502), {"nodes": []}])
    assert rn.locations(CONN) == [] and len(seen) == 2
    server.append(http_error(401))
    with pytest.raises(rn.RetailNextError, match="refused the key") as e:
        rn.locations(CONN)
    assert "SK" not in str(e.value) and len(seen) == 3
    server.extend([urllib.error.URLError("offline")] * rn.RETRIES)
    with pytest.raises(rn.RetailNextError, match="Could not reach"):
        rn.locations(CONN)


def test_the_traffic_query_follows_the_documented_shape() -> None:
    body = rn.traffic_request(["u1"], date(2026, 9, 12), "11:30", "11:45")
    assert body["metrics"] == ["traffic_in", "traffic_out"] and body["locations"] == ["u1"]
    assert body["date_ranges"] == [{"from": {"gregorian": "2026-09-12T00:00:00Z"},
                                    "to": {"gregorian": "2026-09-13T00:00:00Z"}}]
    assert body["time_ranges"] == [{"from": "11:30", "until": "11:45"}]
    assert body["group_bys"] == [{"group": "time", "unit": "minutes", "value": 15}]


def test_the_footage_period_in_whole_intervals() -> None:
    at = datetime.fromisoformat
    assert rn.period_of(at("2026-09-12T11:30:00"), at("2026-09-12T11:45:00")) == (
        date(2026, 9, 12), "11:30", "11:45")
    assert rn.period_of(at("2026-09-12T11:37:10"), at("2026-09-12T11:52:00"))[1:] == ("11:30", "12:00")
    assert rn.period_of(at("2026-09-12T23:50:00"), at("2026-09-13T00:00:00"))[2] == "24:00"
    with pytest.raises(rn.RetailNextError, match="midnight"):
        rn.period_of(at("2026-09-12T23:50:00"), at("2026-09-13T00:10:00"))


def test_the_key_lives_in_the_credential_store(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: store.pop((s, u)))
    assert rn.load_connection() is None
    rn.save_connection("https://acme.api.retailnext.net", " AK ", "SK")
    assert rn.load_connection() == CONN
    settings = (tmp_path / "settings.json").read_text()
    assert "acme" in settings and "SK" not in settings and "AK" not in settings
    rn.forget_connection()
    assert rn.load_connection() is None and not store
