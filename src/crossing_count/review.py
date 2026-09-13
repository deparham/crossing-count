"""M3: human review of the proposals. The reviewer's decisions are the ground truth.

The queue, per camera, in this order:
  candidate    every proposed crossing: accept, reject, split into two, or mark unsure;
               flip the direction if it is wrong; tag child / staff / pram / group / unsure
  discard      every pending_expired and duplicate, plus a seeded random sample of the
               other rejections, so the counting rule is audited rather than trusted:
               confirm the rejection, or restore it as a real crossing
  unexplained  motion near the line that produced no proposal (likely misses first):
               scanned, and any missed crossing added at the moment it happens

Decisions are written to disk after every keypress, so an interrupted session
resumes exactly where it stopped.
"""

from __future__ import annotations

import json
import random
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import write_json_atomic

TAGS = ("child", "staff", "pram/trolley", "group merge", "unsure")
DECISIONS = {
    "candidate": ("accept", "reject", "split", "unsure"),
    "discard": ("confirm", "restore", "unsure"),
    "unexplained": ("scanned", "unsure"),
}
ALWAYS_REVIEW = ("pending_expired", "duplicate")
KIND_ORDER = {"candidate": 0, "discard": 1, "unexplained": 2}
DEFAULT_SECONDS = {"candidate": 6.0, "discard": 6.0}  # per item, before any are timed
SCHEMA = "review/1"


class ReviewError(ValueError):
    pass


@dataclass
class Camera:
    sensor: str
    candidates: dict[str, Any]
    discarded: dict[str, Any]
    unexplained: dict[str, Any]


def load_cameras(run_dir: Path) -> list[Camera]:
    cams: list[Camera] = []
    for d in sorted(p for p in run_dir.iterdir() if p.is_dir() and p.name != "review"):
        if not (d / "candidates.json").is_file():
            continue
        cand = json.loads((d / "candidates.json").read_text())
        cams.append(Camera(cand["config"]["sensor"], cand,
                           json.loads((d / "discarded.json").read_text()),
                           json.loads((d / "unexplained.json").read_text())))
    return cams


def build_queue(cams: list[Camera], seed: int, discard_sample: float) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for cam in cams:
        for c in cam.candidates["candidates"]:
            items.append({"id": c["id"], "camera": cam.sensor, "kind": "candidate",
                          "t": c["t_seconds"], "clip": [c["clip_start"], c["clip_end"]],
                          "direction": c["direction"], "flags": c["flags"]})
        discards = cam.discarded["discarded"]
        must = [d for d in discards if d["reason"] in ALWAYS_REVIEW]
        rest = [d for d in discards if d["reason"] not in ALWAYS_REVIEW]
        rng = random.Random(f"{seed}:{cam.sensor}")
        k = min(len(rest), max(5, round(discard_sample * len(rest))))
        chosen = must + rng.sample(rest, k)
        for d in sorted(chosen, key=lambda d: (d["t_seconds"], d["id"])):
            items.append({"id": d["id"], "camera": cam.sensor, "kind": "discard",
                          "t": d["t_seconds"], "clip": [d["clip_start"], d["clip_end"]],
                          "reason": d["reason"], "pattern": d.get("pattern"),
                          "directions": [x["direction"] for x in d["crossings"]],
                          "crossing_times": [x["t"] for x in d["crossings"]]})
        for u in cam.unexplained["unexplained"]:
            items.append({"id": u["id"], "camera": cam.sensor, "kind": "unexplained",
                          "t": u["start_s"], "clip": [u["start_s"], u["end_s"]],
                          "direction": u.get("direction_guess"), "why": u["kind"]})
    items.sort(key=lambda i: (i["camera"], KIND_ORDER[i["kind"]]))
    return items


class ReviewSession:
    def __init__(self, run_dir: Path, operator: str = "", seed: int = 0,
                 discard_sample: float = 0.25) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "review" / "decisions.json"
        self.cams = load_cameras(run_dir)
        if not self.cams:
            raise ReviewError(f"no detect.py results in {run_dir}; run detect.py first")
        self.by_camera = {c.sensor: c for c in self.cams}
        queue = build_queue(self.cams, seed, discard_sample)
        self.warnings: list[str] = []
        if self.path.is_file():
            state = json.loads(self.path.read_text())
            self.operator = operator or state.get("operator", "")
            self.decisions: dict[str, dict[str, Any]] = state.get("decisions", {})
            self.order: list[str] = state.get("order", [])
            self.drafts: dict[str, dict[str, Any]] = state.get("drafts", {})
            old = {i["id"] for i in state.get("items", [])}
            new = {i["id"] for i in queue}
            if old and old != new:
                self.warnings.append("detect.py results changed since this review began; "
                                     "decisions for items that no longer exist are kept aside")
            self.items = queue if not old or old != new else state["items"]
        else:
            self.operator = operator
            self.decisions = {}
            self.order = []
            self.drafts = {}
            self.items = queue
        self.index = {i["id"]: i for i in self.items}
        self.seed, self.discard_sample = seed, discard_sample
        self.save()

    # --- state -------------------------------------------------------------------------
    def save(self) -> None:
        write_json_atomic(self.path, {
            "schema": SCHEMA, "operator": self.operator, "seed": self.seed,
            "discard_sample": self.discard_sample,
            "video": self.cams[0].candidates["video"],
            "cameras": [c.sensor for c in self.cams],
            "items": self.items, "order": self.order, "decisions": self.decisions,
            "drafts": self.drafts,
        })

    def _clean(self, item_id: str, direction: str | None, tags: list[str] | None,
               added: list[dict[str, Any]] | None
               ) -> tuple[dict[str, Any], str | None, list[str], list[dict[str, Any]]]:
        item = self.index.get(item_id)
        if item is None:
            raise ReviewError(f"unknown item {item_id}")
        clean_tags = list(tags or [])
        bad = [t for t in clean_tags if t not in TAGS]
        if bad:
            raise ReviewError(f"unknown tag(s) {bad}")
        if direction is not None and direction not in ("in", "out"):
            raise ReviewError("direction must be 'in' or 'out'")
        adds = []
        for a in added or []:
            if a.get("direction") not in ("in", "out") or not isinstance(a.get("t"), (int, float)):
                raise ReviewError("each added crossing needs t (seconds) and direction in/out")
            adds.append({"t": round(float(a["t"]), 2), "direction": a["direction"],
                         "tags": [t for t in a.get("tags", []) if t in TAGS]})
        return item, direction or item.get("direction"), clean_tags, adds

    def save_draft(self, item_id: str, *, direction: str | None = None,
                   tags: list[str] | None = None,
                   added: list[dict[str, Any]] | None = None) -> None:
        """Keep work on an undecided item (added crossings, tags, a flip) on every keypress."""
        _, direction, tags, adds = self._clean(item_id, direction, tags, added)
        self.drafts[item_id] = {"direction": direction, "tags": tags, "added": adds,
                                "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        self.save()

    def decide(self, item_id: str, decision: str, *, direction: str | None = None,
               tags: list[str] | None = None, added: list[dict[str, Any]] | None = None,
               seconds: float | None = None) -> dict[str, Any]:
        item, direction, tags, adds = self._clean(item_id, direction, tags, added)
        if decision not in DECISIONS[item["kind"]]:
            raise ReviewError(f"{decision!r} is not a decision for a {item['kind']}")
        self.decisions[item_id] = {
            "decision": decision,
            "direction": direction,
            "tags": tags,
            "added": adds,
            "seconds": None if seconds is None else round(float(seconds), 1),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self.drafts.pop(item_id, None)
        if item_id in self.order:
            self.order.remove(item_id)
        self.order.append(item_id)
        self.save()
        return self.progress()

    def undo(self) -> str | None:
        """Forget the most recent decision; returns the item to show again."""
        while self.order:
            item_id = self.order.pop()
            if self.decisions.pop(item_id, None) is not None:
                self.save()
                return item_id
        return None

    def set_operator(self, name: str) -> None:
        self.operator = name.strip()
        self.save()

    # --- views -------------------------------------------------------------------------
    def next_undecided(self) -> str | None:
        return next((i["id"] for i in self.items if i["id"] not in self.decisions), None)

    def progress(self) -> dict[str, Any]:
        done = [i for i in self.items if i["id"] in self.decisions]
        left = [i for i in self.items if i["id"] not in self.decisions]
        per_kind: dict[str, float] = {}
        for kind in DECISIONS:
            secs = [self.decisions[i["id"]]["seconds"] for i in done
                    if i["kind"] == kind and self.decisions[i["id"]]["seconds"]]
            per_kind[kind] = statistics.median(secs) if secs else DEFAULT_SECONDS.get(kind, 0.0)
        remaining = 0.0
        for i in left:
            if i["kind"] == "unexplained" and not any(
                    j["kind"] == "unexplained" for j in done):
                remaining += 3.0 + (i["clip"][1] - i["clip"][0]) / 2  # scanned at 2x
            else:
                remaining += per_kind[i["kind"]]
        return {"done": len(done), "total": len(self.items), "remaining_s": round(remaining),
                "next": self.next_undecided()}

    def verified(self) -> list[dict[str, Any]]:
        """The ground truth: every crossing a person accepted, restored or added."""
        out: list[dict[str, Any]] = []
        for item in self.items:
            d = self.decisions.get(item["id"])
            if d is None:
                continue
            base = {"camera": item["camera"], "source": item["kind"], "source_id": item["id"],
                    "tags": d["tags"]}
            if item["kind"] == "candidate" and d["decision"] in ("accept", "split"):
                n = 2 if d["decision"] == "split" else 1
                for _ in range(n):
                    out.append({**base, "t": item["t"], "direction": d["direction"]})
            elif item["kind"] == "discard" and d["decision"] == "restore":
                for t, direction in zip(item["crossing_times"], item["directions"]):
                    out.append({**base, "t": t, "direction": direction})
            for a in d["added"]:
                out.append({**base, "source": "added", "t": a["t"], "direction": a["direction"],
                            "tags": a["tags"]})
        return sorted(out, key=lambda c: (c["camera"], c["t"]))

    def counts(self) -> dict[str, Any]:
        """Decision tallies per camera, for the pipeline's own error metrics."""
        out: dict[str, Any] = {}
        for cam in self.cams:
            reviewed = [(i, self.decisions[i["id"]]) for i in self.items
                        if i["camera"] == cam.sensor and i["id"] in self.decisions]
            mine = [i for i in self.items if i["camera"] == cam.sensor]

            def n(kind: str, decision: str | None = None,
                  reviewed: list[tuple[dict[str, Any], dict[str, Any]]] = reviewed) -> int:
                return len([i for i, d in reviewed if i["kind"] == kind
                            and (decision is None or d["decision"] == decision)])

            unexplained_done = [d for i, d in reviewed if i["kind"] == "unexplained"]
            out[cam.sensor] = {
                "candidates": len([i for i in mine if i["kind"] == "candidate"]),
                "candidates_reviewed": n("candidate"),
                "accepted": n("candidate", "accept"),
                "rejected": n("candidate", "reject"),
                "splits": n("candidate", "split"),
                "unsure": n("candidate", "unsure") + n("discard", "unsure")
                + n("unexplained", "unsure"),
                "direction_flipped": len([i for i, d in reviewed if i["kind"] == "candidate"
                                          and d["direction"] != i["direction"]]),
                "discards_reviewed": n("discard"),
                "discards_restored": n("discard", "restore"),
                "unexplained_ranges": len([i for i in mine if i["kind"] == "unexplained"]),
                "unexplained_reviewed": len(unexplained_done),
                "unexplained_with_missed_crossing": len([d for d in unexplained_done
                                                         if d["added"]]),
                "crossings_added": sum(len(d["added"]) for _, d in reviewed),
            }
        return out
