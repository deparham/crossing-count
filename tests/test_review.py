"""M3 review: the queue, decisions, persistence, the verified set, and the page's API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count.review import ReviewError, ReviewSession
from crossing_count.review_app import create_review_app


def test_queue_holds_every_proposal_and_every_unexplained_stretch(fresh: Path) -> None:
    s = ReviewSession(fresh, "tester")
    kinds = [(i["camera"], i["kind"]) for i in s.items]
    assert kinds.count(("CAM-A", "candidate")) == 2 and kinds.count(("CAM-B", "candidate")) == 2
    assert next(k for cam, k in kinds if cam == "CAM-A") == "candidate"  # proposals first
    assert s.progress()["total"] == len(s.items)


def test_decisions_build_the_verified_set(fresh: Path) -> None:
    s = ReviewSession(fresh, "tester")
    a1, a2 = [i["id"] for i in s.items if i["camera"] == "CAM-A" and i["kind"] == "candidate"]
    b1, b2 = [i["id"] for i in s.items if i["camera"] == "CAM-B" and i["kind"] == "candidate"]
    s.decide(a1, "accept", tags=["child"], seconds=4)
    s.decide(a2, "split")
    s.decide(b1, "reject")
    s.decide(b2, "accept", direction="out")  # reviewer flipped it
    unex = next(i["id"] for i in s.items if i["kind"] == "unexplained")
    s.decide(unex, "scanned", added=[{"t": 12.3, "direction": "in"}])
    v = s.verified()
    assert len(v) == 5
    assert [c["direction"] for c in v if c["camera"] == "CAM-B" and c["source"] == "candidate"] == ["out"]
    assert any(c["source"] == "added" and c["t"] == 12.3 for c in v)
    assert next(c for c in v if c["source_id"] == a1)["tags"] == ["child"]
    counts = s.counts()
    assert counts["CAM-A"]["splits"] == 1 and counts["CAM-B"]["direction_flipped"] == 1
    assert sum(c["unexplained_with_missed_crossing"] for c in counts.values()) == 1


def test_decisions_survive_a_restart_and_undo_works(fresh: Path) -> None:
    s = ReviewSession(fresh, "tester")
    first, second = s.items[0]["id"], s.items[1]["id"]
    s.decide(first, "accept")
    s.decide(second, "reject")
    again = ReviewSession(fresh)
    assert again.operator == "tester"
    assert set(again.decisions) == {first, second}
    assert again.next_undecided() == s.items[2]["id"]
    assert again.undo() == second
    assert ReviewSession(fresh).next_undecided() == second


def test_unfinished_work_is_saved_on_every_keypress(fresh: Path) -> None:
    s = ReviewSession(fresh)
    unex = next(i["id"] for i in s.items if i["kind"] == "unexplained")
    s.save_draft(unex, added=[{"t": 5.5, "direction": "out"}], tags=["staff"])
    again = ReviewSession(fresh)
    assert again.drafts[unex]["added"] == [{"t": 5.5, "direction": "out", "tags": []}]
    assert again.verified() == []  # drafts never count until decided
    again.decide(unex, "scanned", added=again.drafts[unex]["added"])
    assert unex not in again.drafts
    assert [c["direction"] for c in again.verified()] == ["out"]


def test_bad_decisions_are_refused(fresh: Path) -> None:
    s = ReviewSession(fresh)
    cand = next(i["id"] for i in s.items if i["kind"] == "candidate")
    with pytest.raises(ReviewError):
        s.decide(cand, "restore")
    with pytest.raises(ReviewError):
        s.decide(cand, "accept", tags=["tourist"])
    with pytest.raises(ReviewError):
        s.decide("nope", "accept")


def test_review_page_api(fresh: Path, two_tile_video: dict[str, Any]) -> None:
    client = TestClient(create_review_app(two_tile_video["video"], fresh, "tester"))
    html = client.get("/").text
    assert "Review" in html and "http://" not in html and "https://" not in html
    sess = client.get("/api/session").json()
    assert set(sess["cameras"]) == {"CAM-A", "CAM-B"}
    assert sess["cameras"]["CAM-A"]["geometry"]["mask_zone"]
    first = sess["items"][0]["id"]
    item = client.get(f"/api/item/{first}").json()
    assert item["record"]["path"]
    p = client.post("/api/decide", json={"id": first, "decision": "accept", "seconds": 3}).json()
    assert p["done"] == 1
    assert client.post("/api/decide", json={"id": first, "decision": "confirm"}).status_code == 400
    assert client.post("/api/undo", json={}).json()["item"] == first
    assert client.post("/api/draft", json={"id": first, "tags": ["child"]}).json()["saved"]
    assert client.get(f"/api/item/{first}").json()["draft"]["tags"] == ["child"]
    r = client.get("/video", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    assert r.headers["content-range"].startswith("bytes 0-99/")
