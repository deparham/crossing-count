"""Proposed counts per camera, with wall-clock times.

These are the pipeline's proposals, before a person has reviewed them. They
are never ground truth: on real footage the proposals alone have missed real
crossings that sat in the rejected and unexplained lists.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _clock(start: datetime | None, t: float) -> str | None:
    return (start + timedelta(seconds=t)).strftime("%H:%M:%S") if start else None


def summarise(run_dir: Path, start: datetime | None) -> dict[str, Any]:
    """Per-camera proposals, rejections and unexplained motion, plus totals."""
    cameras: list[dict[str, Any]] = []
    total = {"in": 0, "out": 0, "rejected": 0, "unexplained": 0}
    for cam_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        f = cam_dir / "candidates.json"
        if not f.is_file():
            continue
        cand = json.loads(f.read_text())
        disc = json.loads((cam_dir / "discarded.json").read_text())
        unex = json.loads((cam_dir / "unexplained.json").read_text())
        s = cand["summary"]
        cam = {
            "sensor": cand["config"]["sensor"],
            "rule": cand["config"]["rule"],
            "in": s["in"],
            "out": s["out"],
            "crossings": [
                {"id": x["id"], "direction": x["direction"], "t": x["t_seconds"],
                 "clock": _clock(start, x["t_seconds"]), "confidence": x["confidence"],
                 "flags": x["flags"]}
                for x in cand["candidates"]
            ],
            "rejected": [
                {"id": x["id"], "reason": x["reason"], "t": x["t_seconds"],
                 "clock": _clock(start, x["t_seconds"]),
                 "directions": [q["direction"] for q in x["crossings"]]}
                for x in disc["discarded"]
            ],
            "unexplained": len(unex["unexplained"]),
            "unexplained_s": s["unexplained_s"],
            "likely_missed": [
                {"id": u["id"], "direction": u["direction_guess"], "t": u["t_seconds"],
                 "clock": _clock(start, u["t_seconds"])}
                for u in unex["unexplained"] if u.get("kind") == "broken_track_at_line"
            ],
            "pending_expired_alarm": s["pending_expired_alarm"],
        }
        cameras.append(cam)
        total["in"] += cam["in"]
        total["out"] += cam["out"]
        total["rejected"] += len(cam["rejected"])
        total["unexplained"] += cam["unexplained"]
    return {"cameras": cameras, "total": total}


def format_summary(summary: dict[str, Any], video: str, start: datetime | None) -> list[str]:
    head = f"PROPOSED crossings, not yet reviewed by a person: {video}"
    lines = [head]
    if start:
        lines.append(f"(video 00:00:00 = {start:%Y-%m-%d %H:%M:%S})")
    for c in summary["cameras"]:
        lines += ["", f"{c['sensor']}  (rule {c['rule']})", f"  IN {c['in']}   OUT {c['out']}"]
        for x in c["crossings"]:
            when = x["clock"] or f"{x['t']:.1f}s"
            flags = f"  [{', '.join(x['flags'])}]" if x["flags"] else ""
            lines.append(f"    {x['id']}  {x['direction'].upper():<3}  {when}  "
                         f"conf {x['confidence']:.2f}{flags}")
        lines.append(f"  rejected by the rule (check these too): {len(c['rejected'])}")
        for x in c["rejected"]:
            when = x["clock"] or f"{x['t']:.1f}s"
            lines.append(f"    {x['id']}  {x['reason']:<20} {'->'.join(x['directions']):<8} {when}")
        lines.append(f"  likely missed crossings (a track lost right at the line): "
                     f"{len(c['likely_missed'])}")
        for x in c["likely_missed"]:
            when = x["clock"] or f"{x['t']:.1f}s"
            lines.append(f"    {x['id']}  probably {x['direction'].upper():<3}  {when}")
        lines.append(f"  unexplained motion near the line: {c['unexplained']} stretches "
                     f"({c['unexplained_s']:.0f} s)")
        if c["pending_expired_alarm"]:
            lines.append("  WARNING: many entries were lost before the mask zone; see discarded.json")
    t = summary["total"]
    lines += ["", (f"TOTAL proposed: IN {t['in']}, OUT {t['out']}   "
                   f"(plus {t['rejected']} rejected tracks and {t['unexplained']} unexplained "
                   f"stretches to review)")]
    return lines
