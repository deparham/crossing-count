"""Several stores' footage queued at once: one window at a time, store by store, while the
person gets on with validating the ones already down.

RetailNext is stubbed at its own edge, so what runs here is the queue itself.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count import retailnext as rn
from crossing_count import sampling
from crossing_count.wizard_app import create_wizard_app

# the words retailnext.windows really uses for a window's edges: "start" and "until".
# The stub said "from" before, agreed with the bug in _item, and passed while queueing
# raised KeyError on the real thing.
WINDOWS = {
    "CN-123": [{"start": "10:45", "until": "11:00", "role": "peak"},
               {"start": "14:00", "until": "14:15", "role": "control"}],
    "JB-401": [{"start": "11:15", "until": "11:30", "role": "peak"}],
    "RW-409": [{"start": "13:15", "until": "13:30", "role": "peak"}],
}


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    state: dict[str, Any] = {"exported": [], "fail": set(), "slow": set()}

    monkeypatch.setattr(rn, "connections", lambda: [rn.Connection("acme", "A", "S")])
    monkeypatch.setattr(rn, "locations", lambda conn: [])
    # the store's uuid is its code, so the stubs below can tell the stores apart
    monkeypatch.setattr(rn, "find_store_in", lambda sets, code: ("acme", {
        "uuid": code.upper(), "store_id": code.upper(), "name": f"{code.upper()} store",
        "time_zone": "Australia/Sydney"}))
    monkeypatch.setattr(rn, "day_traffic", lambda conn, uuid, day, tz=None: [uuid])
    monkeypatch.setattr(rn, "windows", lambda rows, minutes, direction: rows)
    monkeypatch.setattr(sampling, "plan", lambda windows, **kw: {
        "chosen": WINDOWS[windows[0]], "candidates": [], "mode": kw.get("mode")})
    monkeypatch.setattr(sampling, "record", lambda plan, frm, until: {"role": "peak"})
    # the channel's value is the store's code, so the export stub can tell them apart
    monkeypatch.setattr(rn, "video_channels", lambda nodes, uuid: {f"{uuid}-PB1": uuid})
    monkeypatch.setattr(rn, "entrances", lambda nodes, uuid: {})
    monkeypatch.setattr(rn, "local_period", lambda day, frm, until, tz: (
        datetime.fromisoformat(f"{day}T{frm}"), datetime.fromisoformat(f"{day}T{until}")))
    monkeypatch.setattr(rn, "footage_name", lambda code, a, b, marks:
                        f"Export - {code}{' marked' if marks else ''} - {a:%H%M}.mp4")
    monkeypatch.setattr(rn, "free_path", lambda folder, name: folder / name)
    monkeypatch.setattr(rn, "store_summary", lambda sub, nodes, store: {"code": store["store_id"]})
    monkeypatch.setattr(rn, "remember_download", lambda where, info: None)

    def start_export(conn: Any, chans: Any, a: Any, b: Any, marks: bool) -> str:
        code = str(chans[0])
        state["exported"].append(f"{code}@{a:%H:%M}{'+m' if marks else ''}")
        if any(f in code for f in state["fail"]):
            raise rn.RetailNextError(f"RetailNext could not export the video: {code} is down")
        return code

    monkeypatch.setattr(rn, "start_export", start_export)
    monkeypatch.setattr(rn, "export_status", lambda conn, eid: ("ready", {"url": eid}))

    def download(info: Any, where: Path, on: Any) -> None:
        if any(f in str(info) for f in state["slow"]):
            time.sleep(0.4)
        where.write_bytes(b"video")
        on(5, 5)

    monkeypatch.setattr(rn, "download", download)
    footage = tmp_path / "footage"
    footage.mkdir()
    state["client"] = TestClient(create_wizard_app(tmp_path / "sites", tmp_path / "data",
                                                   [footage]))
    return state


def _settled(client: TestClient, seconds: float = 10.0) -> dict[str, Any]:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        got: dict[str, Any] = client.get("/api/retailnext/queue").json()
        if not got["working"]:
            return got
        time.sleep(0.05)
    raise AssertionError("the queue never finished")


def test_one_window_a_store_the_busiest_one(stubs: dict[str, Any]) -> None:
    """Six stores ticked, six videos queued. A store's whole plan would fill the queue
    faster than anyone can count it, so only its busiest window is taken."""
    client = stubs["client"]
    got = client.post("/api/retailnext/queue", json={
        "codes": ["CN-123", "JB-401", "RW-409"], "date": "2026-09-19"}).json()
    assert got["added"] == 3 and got["problems"] == []  # one each, though CN-123 has two
    assert [i["label"].split(" · ")[1] for i in got["items"]] == [
        "10:45–11:00", "11:15–11:30", "13:15–13:30"]
    assert all("peak" in i["label"] for i in got["items"])  # the busiest, never the control

    done = _settled(client)
    assert [i["state"] for i in done["items"]] == ["ready"] * 3
    assert stubs["exported"] == ["CN-123@10:45", "JB-401@11:15", "RW-409@13:15"]
    assert all(Path(i["path"]).is_file() for i in done["items"])


def test_more_than_one_a_store_only_when_it_is_asked_for(stubs: dict[str, Any]) -> None:
    client = stubs["client"]
    got = client.post("/api/retailnext/queue", json={
        "codes": ["CN-123"], "date": "2026-09-19", "per_store": 2}).json()
    assert [i["label"].split(" · ")[2] for i in got["items"]] == ["peak", "control"]
    _settled(client)


def test_one_store_being_down_does_not_stop_the_others(stubs: dict[str, Any]) -> None:
    stubs["fail"].add("JB-401")
    client = stubs["client"]
    client.post("/api/retailnext/queue", json={
        "codes": ["CN-123", "JB-401", "RW-409"], "date": "2026-09-19"})
    done = _settled(client)
    by = {i["code"]: i for i in done["items"]}
    assert by["JB-401"]["state"] == "failed" and "is down" in by["JB-401"]["message"]
    assert by["CN-123"]["state"] == "ready" and by["RW-409"]["state"] == "ready"
    assert len(done["ready"]) == 2  # the other two still came down


def test_the_first_is_ready_while_the_rest_are_still_coming(stubs: dict[str, Any]) -> None:
    """The point of the queue: validate the first while the others download."""
    stubs["slow"].add("RW-409")
    client = stubs["client"]
    client.post("/api/retailnext/queue", json={"codes": ["CN-123", "RW-409"],
                                               "date": "2026-09-19"})
    end = time.monotonic() + 10.0
    while time.monotonic() < end:
        got = client.get("/api/retailnext/queue").json()
        if got["ready"] and got["working"]:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("never had one ready while still working")
    assert Path(got["ready"][0]["path"]).is_file()  # openable now, the rest still coming
    _settled(client)


def test_one_window_on_its_own_uses_the_same_queue(stubs: dict[str, Any]) -> None:
    client = stubs["client"]
    one = client.post("/api/retailnext/download", json={
        "code": "CN-123", "date": "2026-09-19", "start": "10:45", "until": "11:00"}).json()
    assert one["state"] in ("waiting", "exporting", "downloading")  # the worker starts at once
    busy = client.post("/api/retailnext/download", json={
        "code": "CN-123", "date": "2026-09-19", "start": "14:00", "until": "14:15"})
    assert busy.status_code in (200, 409)  # refused while one is under way, as it always was
    _settled(client)
    said = client.get("/api/retailnext/download").json()
    assert said["state"] == "done"  # the old word the footage page waits for
    assert Path(said["path"]).is_file()


def test_taking_one_out_of_the_queue(stubs: dict[str, Any]) -> None:
    client = stubs["client"]
    client.post("/api/retailnext/queue", json={"codes": ["CN-123"], "date": "2026-09-19",
                                               "per_store": 2})
    done = _settled(client)["items"]
    assert len(done) == 2
    left = client.post("/api/retailnext/queue/forget", json={"id": done[0]["id"]}).json()
    assert [i["id"] for i in left["items"]] == [done[1]["id"]]
    assert Path(done[0]["path"]).is_file()  # the footage itself is left where it was
