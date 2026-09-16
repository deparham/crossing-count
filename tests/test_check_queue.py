"""What the check step puts to a person, including the sample that audits the rule itself."""

from __future__ import annotations

from typing import Any

from crossing_count.wizard import AUDIT_MIN, POSSIBLE_REASONS, review_items

CANDIDATES: dict[str, Any] = {"candidates": [
    {"id": "c1", "t_seconds": 10.0, "direction": "in", "clip_start": 8.0, "clip_end": 12.0,
     "crossing_xy": [1.0, 2.0], "path": []}]}
UNEXPLAINED: dict[str, Any] = {"unexplained": []}


def _discarded(n: int) -> dict[str, Any]:
    """One rejection of a kind already known to be often real, and n of other kinds."""
    out = [{"id": "d0", "reason": POSSIBLE_REASONS[0], "path": [],
            "crossings": [{"t": 5.0, "direction": "in"}]}]
    out += [{"id": f"x{k}", "reason": "uturn_no_mask", "path": [],
             "crossings": [{"t": 20.0 + k, "direction": "in"}]} for k in range(n)]
    return {"discarded": out}


def _items(seed: str | None, n: int = 40) -> list[dict[str, Any]]:
    return review_items("CAM-A", 0, CANDIDATES, _discarded(n), UNEXPLAINED, ["in"], 600.0,
                        audit_seed=seed)


def test_a_sample_of_the_rules_other_rejections_is_checked_too() -> None:
    plain = _items(None)
    assert [i["why"] for i in plain] == ["detected", POSSIBLE_REASONS[0]]  # as scoring replays see it
    audited = _items("video-fingerprint")
    sampled = [i for i in audited if i["why"] == "audit"]
    assert len(sampled) == 10  # a quarter of the 40 the rule threw away for other reasons
    assert all(i["kind"] == "possible" and i["direction"] == "in" for i in sampled)
    assert [i["t"] for i in sampled] == sorted(i["t"] for i in sampled)
    # the same footage always asks about the same ones, and another video's sample differs
    def audit_ids(seed: str) -> list[str]:
        return [str(i["id"]) for i in _items(seed) if i["why"] == "audit"]

    assert [i["id"] for i in sampled] == audit_ids("video-fingerprint")
    assert [i["id"] for i in sampled] != audit_ids("another-video")


def test_a_few_rejections_are_all_checked_and_none_is_invented() -> None:
    few = [i for i in _items("seed", n=3) if i["why"] == "audit"]
    assert len(few) == 3  # fewer than the minimum: all of them, never more than there are
    some = [i for i in _items("seed", n=AUDIT_MIN + 3) if i["why"] == "audit"]
    assert len(some) == AUDIT_MIN  # a quarter would be too few to audit anything
    assert not [i for i in _items("seed", n=0) if i["why"] == "audit"]
