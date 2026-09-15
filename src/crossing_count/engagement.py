"""Several finalised windows of one store, put together: an engagement.

One short window rarely has enough verified crossings for a percentage
(validation.quote), and never an uncertainty range: that needs at least
validation.MIN_CLUSTERS windows, resampled whole. Putting a store's finalised windows
together is the intended route to a quotable figure, and the place where peak windows and
their control windows are set side by side, level by level (sampling.py).

Only finalised validations take part (runs.py: an ID, files checked against the manifest),
all of one store and one counting system. Incomplete ones, ones counted on footage showing
the system's own marks, changed ones and ones replaced by a newer version also chosen are
listed with the reason, not used. An engagement can be kept, read-only, in
<data>/engagements/<ID>/engagement.json, with the IDs and manifest checksums it was made from.
"""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import auditlog, independence, provenance, runs, validation

PREFIX = "CC-ENG"
SCHEMA = "engagement/1"
ID_RE = re.compile(rf"^{PREFIX}-(\d{{4}})-([0-9A-F]{{4}})-(\d{{6}})$")


class EngagementError(Exception):
    """Windows that cannot be put together, said plainly."""


def engagements_dir(root: Path) -> Path:
    return root / "engagements"


def _load(root: Path, vid: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    name = Path(vid).name
    folder = runs.runs_dir(root) / name
    if not runs.ID_RE.match(name) or not folder.is_dir():
        return None, None, "no such finalised validation on this computer"
    try:
        m = runs.read(folder)
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    except (runs.RunError, OSError, ValueError) as e:
        return None, None, f"unreadable ({e})"
    if not runs.verify(folder, root)["intact"]:
        return m, result, "its files changed since it was finalised"
    return m, independence.apply(result, independence.registry(root)), None


def gather(root: Path, ids: Iterable[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The windows that can be used, and those that cannot, with why."""
    loaded = {vid: _load(root, vid) for vid in dict.fromkeys(str(i) for i in ids)}
    replaced = {str(m["supersedes"]): vid for vid, (m, _, _) in loaded.items()
                if m and m.get("supersedes")}
    used: list[dict[str, Any]] = []
    left: list[dict[str, Any]] = []
    for vid, (m, result, problem) in loaded.items():
        why = [problem] if problem else []
        if m is not None and result is not None and not problem:
            why += validation.exclusions(result)
            if vid in replaced:
                why.append(f"replaced by {replaced[vid]}, also chosen")
        (left if why else used).append({"id": vid, "manifest": m, "result": result, "why": why})
    return used, left


def _accuracy(truth: int, system: int) -> float | None:
    return round(max(0.0, 100.0 - abs(100.0 * (system - truth) / truth)), 1) if truth else None


def summarise(root: Path, ids: Iterable[str]) -> dict[str, Any]:
    """The windows put together: per direction what may be said (validation.quote), the
    bias with its range resampling whole windows, the error by traffic level, and how the
    windows were sampled."""
    used, left = gather(root, ids)
    if not used:
        raise EngagementError("None of these can be used: " + "; ".join(
            f"{x['id']}: {', '.join(x['why'])}" for x in left) + "." if left
            else "Choose the finalised windows to put together.")
    stores = sorted({str((u["manifest"].get("store") or {}).get("code")) for u in used})
    if len(stores) > 1:
        raise EngagementError(f"An engagement is one store's windows: these are from "
                              f"{', '.join(stores)}.")
    systems = sorted({str(u["result"].get("system") or "?") for u in used})
    if len(systems) > 1:
        raise EngagementError(f"An engagement compares one counting system: these are "
                              f"{', '.join(systems)}.")
    system = systems[0]
    vals = [{"id": u["id"], "store": stores[0], "sampling": u["result"].get("sampling"),
             "camera_hours": float(u["result"].get("duration_s") or 0)
             * len(u["result"].get("cameras") or [1]) / 3600, "rows": u["result"]["rows"]}
            for u in used]
    s = validation.summary(vals)
    directions: dict[str, dict[str, Any]] = {}
    for d, m in s["by_direction"].items():
        q = validation.quote(int(m["truth"]), int(m["system"]))
        rng = m["ranges"]["bias_pct"]["by_validation"]
        directions[d] = {**q, "bias_pct": m["bias_pct"] if q["rate"] else None,
                         "accuracy_pct": _accuracy(q["truth"], int(m["system"])) if q["rate"] else None,
                         "range": rng if q["rate"] else None,
                         "range_note": None if rng or not q["rate"] else
                         f"no range: fewer than {validation.MIN_CLUSTERS} windows"}

    def window(u: dict[str, Any]) -> dict[str, Any]:
        r = u["result"]
        return {"id": u["id"], "clock_start": (u["manifest"].get("footage") or {}).get("clock_start"),
                "role": (r.get("sampling") or {}).get("role") or "chosen by hand",
                "level": (r.get("traffic") or {}).get("level"),
                "verified": sum(int(x["truth"]) for x in r["rows"]),
                "system": sum(int(x["system"]) for x in r["rows"])}

    return {"schema": SCHEMA, "store": used[0]["manifest"].get("store") or {"code": stores[0]},
            "system": system, "windows": [window(u) for u in used],
            "left": [{"id": x["id"], "why": x["why"]} for x in left],
            "directions": directions, "summary": s, "headline": validation.headline(s, system),
            "levels": validation.level_sentence(s["by_traffic"], system)}


def _allocate(root: Path, year: int) -> tuple[str, Path]:
    base = engagements_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    tag = runs.computer_tag()
    n = max((int(m[3]) for p in base.iterdir()
             if (m := ID_RE.match(p.name)) and int(m[1]) == year and m[2] == tag), default=0) + 1
    while True:
        eid = f"{PREFIX}-{year}-{tag}-{n:06d}"
        try:
            (base / eid).mkdir()
        except FileExistsError:
            n += 1
            continue
        return eid, base / eid


def write(root: Path, ids: Iterable[str], by: str = "") -> dict[str, Any]:
    """Keep an engagement under its own ID, read-only, naming the manifests it rests on."""
    e = summarise(root, ids)
    now = datetime.now().astimezone()
    eid, folder = _allocate(root, now.year)
    rests_on = [{"id": w["id"], "manifest_sha256": provenance.file_sha256(
        runs.runs_dir(root) / w["id"] / "manifest.json")} for w in e["windows"]]
    rec = {"schema": SCHEMA, "engagement_id": eid, "made_at": now.isoformat(timespec="seconds"),
           "by": by, "validations": rests_on, "engagement": e}
    p = folder / "engagement.json"
    p.write_text(json.dumps(rec, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    auditlog.append("engagement_made", user=by, obj={"engagement": eid},
                    after={"validations": [r["id"] for r in rests_on]}, root=root)
    return {**e, "engagement_id": eid, "folder": str(folder)}
