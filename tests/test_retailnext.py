"""RetailNext's API, against a fake server: no test reaches the network or a real key."""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Self

import keyring
import keyring.errors
import pytest
from fastapi.testclient import TestClient

from crossing_count import retailnext as rn
from crossing_count import video as vid
from crossing_count import wizard_app
from crossing_count.wizard_app import create_wizard_app


def deleter(store: dict[tuple[str, str], str]) -> Any:
    """keyring.delete_password over a dict, refusing a missing entry as the real one does."""
    def delete(service: str, name: str) -> None:
        if (service, name) not in store:
            raise keyring.errors.PasswordDeleteError("Password not found")
        del store[(service, name)]
    return delete


class Reply:
    def __init__(self, body: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.buf = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        return self.buf.read(n)


def http_error(code: int, text: str = "", headers: dict[str, str] | None = None
               ) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", headers or {},  # type: ignore[arg-type]
                                  io.BytesIO(text.encode()))


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
        return a if isinstance(a, Reply) else Reply(a)

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


def test_pasting_marks_are_removed_and_odd_keys_flagged() -> None:
    assert rn.clean_key("\x1b[200~Ab-9_x\x1b[201~\n") == "Ab-9_x"
    ok = rn.Connection("acme", "fb38f55f-0000-11f1-997f-0000dea53117", "Abc123_-Abc123_-Abc123")
    assert rn.key_problems(ok) == []
    swapped = rn.Connection("acme", ok.secret_key, ok.access_key)
    assert "swapped" in rn.key_problems(swapped)[0]
    assert any("other than letters" in p for p in rn.key_problems(
        rn.Connection("acme", ok.access_key, "Abc123!Abc123_-Abc123")))


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


def test_a_store_time_zone_makes_the_day_the_stores_own() -> None:
    body = rn.traffic_request(["u1"], date(2026, 9, 12), "11:30", "11:45",
                              time_zone="Australia/Brisbane")
    assert body["time_zone"] == "Australia/Brisbane"
    assert body["date_ranges"] == [{"from": {"gregorian": "2026-09-11T14:00:00Z"},
                                    "to": {"gregorian": "2026-09-12T14:00:00Z"}}]


def test_the_answer_as_rows_with_validity() -> None:
    def point(start: str, finish: str, value: int, validity: str = "complete") -> dict[str, Any]:
        return {"value": value, "validity": validity, "index": 0,
                "group": {"type": "time", "start": start, "finish": finish}}

    answer = {"ok": True, "metrics": [
        {"name": "traffic_in", "ok": True, "data": [point("11:30", "11:45", 20),
                                                     point("11:45", "12:00", 7, "imputed")]},
        {"name": "traffic_out", "ok": True, "data": [point("11:30", "11:45", 23),
                                                      point("11:45", "12:00", 9)]}]}
    rows = rn.traffic_table(answer)
    assert [(r["start"], r["in"], r["out"], r["validity"]) for r in rows] == [
        ("11:30", 20, 23, "complete"), ("11:45", 7, 9, "imputed")]
    with pytest.raises(rn.RetailNextError, match="location profile"):
        rn.traffic_table({"ok": True, "metrics": [{"name": "traffic_in", "ok": False}]})


NODES: list[dict[str, Any]] = [
    {"uuid": "s1", "location_type": "store", "store_id": "CN-123", "name": "Tweed Heads CN-123",
     "time_zone": "Australia/Sydney"},
    {"uuid": "e1", "location_type": "entrance", "parent_uuid": "s1", "name": "CN-123-PB1"},
    {"uuid": "e2", "location_type": "entrance", "parent_uuid": "s1", "name": "CN-123-R2"},
    {"uuid": "t1", "type": "traffic", "parent_uuid": "e1", "name": "CN-123-PB1 Traffic 1"},
    {"uuid": "v1", "type": "video", "parent_uuid": "e1", "name": "CN-123-PB1"},
    {"uuid": "v2", "type": "video", "parent_uuid": "e2", "name": "CN-123-R2"},
]


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[tuple[str, str], str]:
    """A credential store in memory, and a data folder of the test's own."""
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path))
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", deleter(store))
    return store


def test_every_connected_subscription_is_kept(credentials: dict[tuple[str, str], str],
                                              tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text('{"retailnext_subscription": "old"}')  # one, as before
    credentials[(rn.SERVICE, "old")] = json.dumps({"access_key": "A0", "secret_key": "S0"})
    rn.save_connection("rag", "A1", "S1")
    rn.save_connection("https://gazman.cloud.retailnext.net", "A2", "S2")
    assert rn.subscriptions() == ["old", "rag", "gazman"]
    assert [c.subscription for c in rn.connections()] == ["old", "rag", "gazman"]
    assert rn.load_connection("rag") == rn.Connection("rag", "A1", "S1")
    assert rn.load_connection() == rn.Connection("gazman", "A2", "S2")  # the last connected
    rn.forget_connection("rag")
    assert rn.subscriptions() == ["old", "gazman"] and (rn.SERVICE, "rag") not in credentials


def test_a_key_from_the_page_is_kept_only_if_retailnext_accepts_it(
        server: list[Any], credentials: dict[tuple[str, str], str]) -> None:
    server.pop(0)
    server.append(http_error(401, "401 bad password"))
    with pytest.raises(rn.RetailNextError, match="refused the key") as e:
        rn.connect("gazman", "fb38f55f-0000-11f1-997f-0000dea53117", "Abc]defAbcdefAbcdef")
    assert "position 4" in str(e.value) and "Abc]def" not in str(e.value)
    assert not credentials and rn.subscriptions() == []  # nothing kept
    server.append({"nodes": [[{"uuid": "s1", "location_type": "store"}]]})
    conn = rn.connect("https://gazman.cloud.retailnext.net", " AK ", "SK")
    assert conn.subscription == "gazman" and rn.subscriptions() == ["gazman"]
    assert rn.load_connection("gazman") == rn.Connection("gazman", "AK", "SK")


def test_a_store_is_found_in_whichever_subscription_has_it() -> None:
    gazman = [{"uuid": "g1", "location_type": "store", "store_id": "GZ-001", "name": "Gazman One"}]
    assert rn.find_store_in({"rag": NODES, "gazman": gazman}, "GZ-001")[0] == "gazman"
    assert rn.find_store_in({"rag": NODES, "gazman": gazman}, "cn-123")[0] == "rag"
    both = {"rag": NODES, "acme": NODES}
    with pytest.raises(rn.RetailNextError, match="rag/CN-123"):
        rn.find_store_in(both, "CN-123")
    assert rn.find_store_in(both, "acme/CN-123")[0] == "acme"
    with pytest.raises(rn.RetailNextError, match="None of the connected"):
        rn.find_store_in({"rag": NODES, "gazman": gazman}, "ZZ-404")


ARMADALE: list[dict[str, Any]] = [  # a store keeping its camera's video under the store itself
    {"uuid": "s2", "location_type": "store", "store_id": "392", "name": "392 Perri Cutten Armadale",
     "time_zone": "Australia/Hobart"},
    {"uuid": "vA", "type": "video", "parent_uuid": "s2", "name": "Armadale_Entrance"},
    {"uuid": "eA", "location_type": "entrance", "parent_uuid": "s2", "name": "Entrance"},
    {"uuid": "tA", "type": "traffic", "parent_uuid": "eA", "name": "Perri Cutten Armadale Traffic 1"},
]


def test_a_store_whose_entrances_are_not_named_like_its_cameras(server: list[Any]) -> None:
    seen = server.pop(0)
    assert rn.video_channels(ARMADALE, "s2") == {"Armadale_Entrance": "vA"}
    assert rn.video_channels(NODES, "s1") == {"CN-123-PB1": "v1", "CN-123-R2": "v2"}
    group = {"type": "time", "start": "11:00", "finish": "11:15"}
    server.append({"ok": True, "metrics": [
        {"name": "traffic_in", "ok": True, "data": [{"value": 4, "validity": "complete", "group": group}]},
        {"name": "traffic_out", "ok": True, "data": [{"value": 6, "validity": "complete", "group": group}]}]})
    at = datetime.fromisoformat
    got = rn.camera_counts(CONN, ARMADALE, "392", ["Armadale_Entrance"],
                           at("2026-09-12T11:00:00"), at("2026-09-12T11:15:00"))
    assert got["cameras"] == {} and got["total"][0]["out"] == 6 and "total" in got["note"]
    assert json.loads(seen[0].data)["locations"] == ["s2"]  # the whole store: all its cameras
    with pytest.raises(rn.RetailNextError, match="no entrance called Back_Door"):
        rn.camera_counts(CONN, ARMADALE, "392", ["Back_Door"],
                         at("2026-09-12T11:00:00"), at("2026-09-12T11:15:00"))


def test_what_a_downloaded_video_is(credentials: dict[tuple[str, str], str],
                                    tmp_path: Path) -> None:
    video = tmp_path / "Export - 392 marked - 2026-09-13-124500 AEST to 2026-09-13-130000 AEST.mp4"
    assert rn.download_info(video) == {"code": "392", "marks": True}  # from the name only
    assert rn.download_info(tmp_path / "Export - Multiple Channels - 2026-09-12-113000 AEST to "
                                       "2026-09-12-114500 AEST.mp4") is None
    rn.remember_download(video, rn.store_summary("rag", NODES, NODES[0]))
    info = rn.download_info(video)
    assert info is not None and info["subscription"] == "rag" and info["code"] == "CN-123"
    assert info["cameras"] == ["CN-123-PB1", "CN-123-R2"]


def test_the_busiest_window_for_the_traffic_validated() -> None:
    def row(start: str, finish: str, i: int, o: int, validity: str = "complete") -> dict[str, Any]:
        return {"start": start, "finish": finish, "in": i, "out": o, "validity": validity}

    rows = [row("10:00", "10:15", 5, 1), row("10:15", "10:30", 9, 2), row("10:30", "10:45", 1, 9),
            row("10:45", "11:00", 2, 8, "imputed"), row("11:30", "11:45", 3, 3)]  # a gap at 11:00
    assert [w["start"] for w in rn.busiest(rows, 15, "out")] == ["10:30", "10:45", "11:30"]
    top = rn.busiest(rows, 30, "out")
    assert (top[0]["start"], top[0]["until"], top[0]["out"], top[0]["validity"]) == (
        "10:30", "11:00", 17, "imputed")
    assert [w["start"] for w in top] == ["10:30", "10:00"]  # no overlap; none across the gap
    assert [(w["start"], w["in"]) for w in rn.busiest(rows, 30, "in", top=1)] == [("10:00", 14)]
    assert [w["start"] for w in rn.busiest(rows, 30, "both")] == ["10:15"]
    with pytest.raises(rn.RetailNextError, match="multiple of 15"):
        rn.busiest(rows, 20, "out")


def test_footage_is_exported_then_downloaded_without_the_key(
        server: list[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = server.pop(0)
    start, end = rn.local_period(date(2026, 9, 12), "11:30", "11:45", "Australia/Sydney")
    server.append(Reply({"ok": True}, 201, {"Location": "/v1/video/export/task/abc123"}))
    assert rn.start_export(CONN, ["v1", "v2"], start, end, marks=False) == "abc123"
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0].full_url).query)
    assert seen[0].get_method() == "POST" and q["channel"] == ["v1", "v2"]
    assert (q["start"], q["end"]) == (["2026-09-12T01:30:00Z"], ["2026-09-12T01:45:00Z"])
    assert q["analytics_overlay"] == ["0"] and q["info_overlay"] == ["1"]
    server.extend([Reply({"status": "NotDone"}, 202),
                   http_error(302, headers={"Location": "https://storage.example/f.mp4?sig=1"}),
                   http_error(424, '{"status": "Failed", "failure_reason": "No recorded video"}')])
    assert rn.export_status(CONN, "abc123") == ("working", "")
    assert rn.export_status(CONN, "abc123") == ("ready", "https://storage.example/f.mp4?sig=1")
    assert rn.export_status(CONN, "abc123") == ("failed", "No recorded video")

    fetched: list[urllib.request.Request] = []

    def fake_download(req: urllib.request.Request, timeout: float, context: Any) -> Reply:
        fetched.append(req)
        return Reply(b"video-bytes", 200, {"Content-Length": "11"})

    monkeypatch.setattr(rn, "_download_open", fake_download)
    name = rn.footage_name("CN-123", start, end, marks=False)
    assert name == "Export - CN-123 - 2026-09-12-113000 AEST to 2026-09-12-114500 AEST.mp4"
    (tmp_path / name).write_bytes(b"footage already there")
    dest = rn.free_path(tmp_path, name)
    assert dest.name.endswith("AEST (2).mp4")  # never overwritten
    rn.download("https://storage.example/f.mp4?sig=1", dest)
    assert dest.read_bytes() == b"video-bytes" and not fetched[0].has_header("Authorization")
    span = vid.parse_filename_interval(dest.name)
    assert span is not None and span.start == "2026-09-12T11:30:00" and span.tz == "AEST"


def test_the_app_finds_the_busiest_time_and_downloads_it(monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text('{"retailnext_subscription": "acme"}')
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(home))
    monkeypatch.setattr(rn, "connections", lambda: [CONN])
    monkeypatch.setattr(rn, "locations", lambda conn, types=None: NODES)
    rows = [{"start": f"11:{m:02d}", "finish": f"11:{m + 15:02d}", "in": 5, "out": o,
             "validity": "complete"} for m, o in ((0, 2), (15, 9), (30, 4))]
    monkeypatch.setattr(rn, "day_traffic", lambda conn, uuid, day, tz, minutes=15: rows)
    exports: list[tuple[list[str], bool]] = []

    def fake_start(conn: Any, channels: list[str], start: Any, end: Any, marks: bool) -> str:
        exports.append((channels, marks))
        return "x1"

    def fake_download(url: str, dest: Path, progress: Any = None) -> Path:
        dest.write_bytes(b"v")
        return dest

    monkeypatch.setattr(rn, "start_export", fake_start)
    monkeypatch.setattr(rn, "export_status", lambda conn, export_id: ("ready", "https://s/f.mp4"))
    monkeypatch.setattr(rn, "download", fake_download)
    footage = tmp_path / "footage"
    footage.mkdir()
    client = TestClient(create_wizard_app(tmp_path, tmp_path / "runs", [footage]))
    brands = client.get("/api/retailnext").json()
    assert (brands["connected"], brands["subscriptions"], brands["builtin"]) == (True, ["acme"], [])
    monkeypatch.setattr(rn, "connect", lambda sub, access, secret: CONN)
    added = client.post("/api/retailnext/connect", json={
        "subscription": "acme", "access_key": "AK-typed", "secret_key": "SK-typed"})
    assert added.status_code == 200 and added.json()["subscription"] == "acme"
    assert "SK-typed" not in added.text and "AK-typed" not in added.text  # never sent back
    forgotten: list[str | None] = []
    monkeypatch.setattr(rn, "forget_connection", lambda sub=None: forgotten.append(sub))
    assert client.post("/api/retailnext/forget", json={"subscription": "Acme"}).status_code == 200
    assert forgotten == ["acme"]
    stopped: list[int] = []
    monkeypatch.setattr(wizard_app, "_stop_server", lambda: stopped.append(1))
    assert client.post("/api/quit").json() == {"ok": True}
    for _ in range(50):
        if stopped:
            break
        time.sleep(0.05)
    assert stopped == [1]  # the app closes itself, after answering
    assert client.get("/api/retailnext/stores").json()["stores"] == [
        {"code": "CN-123", "name": "Tweed Heads CN-123", "subscription": "acme"}]
    assert [s["code"] for s in client.get(
        "/api/retailnext/stores", params={"subscription": "acme"}).json()["stores"]] == ["CN-123"]
    assert client.get("/api/retailnext/stores", params={"subscription": "other"}).status_code == 400
    found = client.post("/api/retailnext/busiest", json={  # the brand chosen on the page
        "code": "acme/CN-123", "date": "2026-09-12", "minutes": 15, "direction": "out"}).json()
    assert (found["windows"][0]["start"], found["windows"][0]["out"]) == ("11:15", 9)
    assert (found["sampling"]["mode"], found["considered"]) == ("peak", 3)  # the default: peak
    client.post("/api/retailnext/download", json={"code": "CN-123", "date": "2026-09-12",
                                                  "start": "11:15", "until": "11:30", "marks": True})
    job: dict[str, Any] = {}
    for _ in range(200):
        job = client.get("/api/retailnext/download").json()
        if job["state"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert job["state"] == "done", job
    assert Path(job["path"]).name == (
        "Export - CN-123 marked - 2026-09-12-111500 AEST to 2026-09-12-113000 AEST.mp4")
    assert exports == [(["v1", "v2"], True)]  # every camera; RetailNext's marks for a hand count
    kept = rn.download_info(Path(job["path"]))  # which window of the day it is, and why
    assert kept is not None and kept["sampling"]["role"] == "peak"
    assert (kept["sampling"]["window"]["start"], kept["sampling"]["seed"]) == (
        "11:15", found["sampling"]["seed"])


def test_each_camera_is_asked_at_its_own_entrance(server: list[Any]) -> None:
    seen = server.pop(0)

    def answer(i: int, o: int) -> dict[str, Any]:
        group = {"type": "time", "start": "11:30", "finish": "11:45"}
        return {"ok": True, "metrics": [
            {"name": "traffic_in", "ok": True, "data": [{"value": i, "validity": "complete", "group": group}]},
            {"name": "traffic_out", "ok": True, "data": [{"value": o, "validity": "complete", "group": group}]}]}

    at = datetime.fromisoformat  # footage clock times are the store's own, without a zone
    server.extend([answer(15, 13), answer(5, 10)])
    got = rn.camera_counts(CONN, NODES, "cn-123", ["CN-123-PB1", "cn-123-r2"],
                           at("2026-09-12T11:30:00"), at("2026-09-12T11:45:00"))
    assert got["store"] == "Tweed Heads CN-123" and got["time_zone"] == "Australia/Sydney"
    assert got["cameras"]["CN-123-PB1"][0]["in"] == 15 and got["cameras"]["cn-123-r2"][0]["out"] == 10
    assert [json.loads(r.data)["locations"] for r in seen] == [["e1"], ["e2"]]
    assert json.loads(seen[0].data)["time_zone"] == "Australia/Sydney"
    with pytest.raises(rn.RetailNextError, match="no entrance called CN-123-L9"):
        rn.camera_counts(CONN, NODES, "CN-123", ["CN-123-L9"], at("2026-09-12T11:30:00"),
                         at("2026-09-12T11:45:00"))
    with pytest.raises(rn.RetailNextError, match="no store"):
        rn.find_store(NODES, "YD-999")


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
    monkeypatch.setattr(keyring, "delete_password", deleter(store))
    assert rn.load_connection() is None
    rn.save_connection("https://acme.api.retailnext.net", " AK ", "SK")
    assert rn.load_connection() == CONN
    settings = (tmp_path / "settings.json").read_text()
    assert "acme" in settings and "SK" not in settings and "AK" not in settings
    rn.forget_connection()
    assert rn.load_connection() is None and not store
