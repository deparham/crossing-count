"""Several stores' footage queued at once: one window at a time, store by store, while the
person gets on with validating the ones already down.

RetailNext is stubbed at its own edge, so what runs here is the queue itself.
"""

from __future__ import annotations

import functools
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count import retailnext as rn
from crossing_count import sampling
from crossing_count.util import default_run_dir
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
    # "held" waits on the gate instead of sleeping, so a test can hold a download open for
    # as long as it needs rather than hoping a sleep is long enough on whatever machine runs it
    state: dict[str, Any] = {"exported": [], "fail": set(), "held": set(),
                             "gate": threading.Event()}

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
        if any(f in str(info) for f in state["held"]):
            assert state["gate"].wait(timeout=10), "the test never let the download go"
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
    """The whole point of the queue: count the first while the others are still coming.

    The second download is held open until this test lets it go, so what is checked is the
    queue's doing and not how fast the machine happens to be.
    """
    stubs["held"].add("RW-409")
    client = stubs["client"]
    client.post("/api/retailnext/queue", json={"codes": ["CN-123", "RW-409"],
                                               "date": "2026-09-19"})
    end = time.monotonic() + 10.0
    while time.monotonic() < end:  # the first lands; the second is held at the gate
        got = client.get("/api/retailnext/queue").json()
        if got["ready"]:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("the first one never came down")
    assert got["working"] is True  # and the queue is still going, by the gate
    assert Path(got["ready"][0]["path"]).is_file()  # openable now, the rest still coming
    assert got["ready"][0]["code"] == "CN-123"
    stubs["gate"].set()
    assert len(_settled(client)["ready"]) == 2


def test_one_window_on_its_own_uses_the_same_queue(stubs: dict[str, Any]) -> None:
    client = stubs["client"]
    one = client.post("/api/retailnext/download", json={
        "code": "CN-123", "date": "2026-09-19", "start": "10:45", "until": "11:00"}).json()
    # never what state it is in by now: the worker starts at once, and on a quick machine a
    # stubbed download is over before the answer is even written out
    assert one["code"] == "CN-123" and one["start"] == "10:45" and one["until"] == "11:00"
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


def _validated(root: Path, video: str) -> None:
    """Write the record a finished validation leaves behind for that footage."""
    where = root / "data" / default_run_dir(video, None) / "wizard"
    where.mkdir(parents=True, exist_ok=True)
    (where / "state.json").write_text(json.dumps({"report": {"path": "x.pptx"}}))


def test_the_one_just_counted_is_not_offered_again(stubs: dict[str, Any],
                                                   tmp_path: Path) -> None:
    """Finish one and the queue moves past it. It used to stay "ready" for ever, so the page
    offered the first of them again the moment you opened the second: round and round between
    two windows, never reaching the third."""
    client = stubs["client"]
    client.post("/api/retailnext/queue", json={
        "codes": ["CN-123", "JB-401", "RW-409"], "date": "2026-09-19"})
    done = _settled(client)
    assert [i["counted"] for i in done["items"]] == [False, False, False]

    _validated(tmp_path, done["items"][0]["path"])
    after = client.get("/api/retailnext/queue").json()
    assert [i["counted"] for i in after["items"]] == [True, False, False]
    assert [i["code"] for i in after["ready"] if not i["counted"]] == ["JB-401", "RW-409"]

    _validated(tmp_path, done["items"][1]["path"])
    last = client.get("/api/retailnext/queue").json()
    assert [i["code"] for i in last["ready"] if not i["counted"]] == ["RW-409"]


def test_footage_opened_but_not_finished_is_still_offered(stubs: dict[str, Any],
                                                          tmp_path: Path) -> None:
    """Counted means a report was made, not merely opened: somebody who starts one and stops
    should still be pointed back at it."""
    client = stubs["client"]
    client.post("/api/retailnext/queue", json={"codes": ["CN-123"], "date": "2026-09-19"})
    done = _settled(client)
    where = tmp_path / "data" / default_run_dir(done["items"][0]["path"], None) / "wizard"
    where.mkdir(parents=True, exist_ok=True)
    (where / "state.json").write_text(json.dumps({"report": None}))  # opened, nothing finished
    assert client.get("/api/retailnext/queue").json()["items"][0]["counted"] is False


# the shape sampling.plan gives back under each rule, with the busiest window in the middle
# of the list so that taking the first of them is plainly not the same as taking the busiest
STRATA = [
    {"start": "07:00", "until": "07:15", "role": "stratum", "rank": 1, "score": 12,
     "level": "quiet", "from_min": 420},
    {"start": "12:15", "until": "12:30", "role": "stratum", "rank": 1, "score": 140,
     "level": "heavy", "from_min": 735},
    {"start": "16:00", "until": "16:15", "role": "stratum", "rank": 1, "score": 65,
     "level": "busy", "from_min": 960},
]
DRAWN = [
    {"start": "09:00", "until": "09:15", "role": "random", "rank": 1, "score": 30,
     "from_min": 540},
    {"start": "13:00", "until": "13:15", "role": "random", "rank": 2, "score": 110,
     "from_min": 780},
]


def _planned(chosen: list[dict[str, Any]], windows: Any, **kw: Any) -> dict[str, Any]:
    return {"chosen": chosen, "candidates": [], "mode": kw.get("mode")}


def test_the_busiest_window_is_queued_whatever_the_rule(stubs: dict[str, Any],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """One window a store, and it is meant to be the busiest. Under the stratified rule every
    chosen window has the same role and rank, so going by those took whichever came first -
    and the strata are built quiet upwards, so that was the quietest window of the day."""
    client = stubs["client"]
    for rule, chosen, want in (("stratified", STRATA, "12:15"), ("random", DRAWN, "13:00")):
        monkeypatch.setattr(sampling, "plan", functools.partial(_planned, chosen))
        got = client.post("/api/retailnext/queue", json={
            "codes": ["CN-123"], "date": "2026-09-19", "mode": rule}).json()
        assert [i["start"] for i in got["items"]] == [want], rule
        client.post("/api/retailnext/queue/forget", json={"id": got["items"][0]["id"]})
        _settled(client)


def test_more_than_one_comes_down_busiest_first(stubs: dict[str, Any],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sampling, "plan", functools.partial(_planned, STRATA))
    got = stubs["client"].post("/api/retailnext/queue", json={
        "codes": ["CN-123"], "date": "2026-09-19", "mode": "stratified",
        "per_store": 3}).json()
    assert [i["start"] for i in got["items"]] == ["12:15", "16:00", "07:00"]
    _settled(stubs["client"])
