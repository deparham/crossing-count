"""M4: export the verified counts, the pipeline's own error metrics, and a report.

Only crossings a person accepted, restored or added in review count. Until the
review is finished the verified counts can only be too low, and every output
says so.
"""

from __future__ import annotations

import csv
import html
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import load_config
from .review import ReviewSession
from .util import fmt_hms, write_json_atomic

CSV_COLUMNS = ["video_time", "video_seconds", "clock_time", "direction", "tags"]
INTERVAL_MIN = 15


@dataclass
class SensorCounts:
    """The sensor's own numbers for the same period, per camera and/or combined."""

    per_camera: dict[str, dict[str, int]] = field(default_factory=dict)
    total: dict[str, int] | None = None

    def is_empty(self) -> bool:
        return not self.per_camera and not self.total


def parse_sensor_arg(value: str) -> tuple[str | None, dict[str, int]]:
    """'CN-123-PB1:in=12,out=14' -> ('CN-123-PB1', {...}); 'in=20,out=23' -> (None, {...})."""
    camera, _, spec = value.rpartition(":")
    counts: dict[str, int] = {}
    for part in spec.split(","):
        key, _, num = part.partition("=")
        key = key.strip().lower()
        if key not in ("in", "out") or not num.strip().isdigit():
            raise ValueError(f"expected in=N,out=M, got {value!r}")
        counts[key] = int(num)
    return (camera or None), counts


def interval_start(t: datetime, minutes: int = INTERVAL_MIN) -> datetime:
    return t.replace(minute=t.minute - t.minute % minutes, second=0, microsecond=0)


def sensor_accuracy(sensor: int | None, truth: int) -> dict[str, Any]:
    if sensor is None:
        return {"sensor": None, "verified": truth, "error": None, "error_pct": None,
                "accuracy_pct": None}
    err = sensor - truth
    if truth == 0:  # nobody really crossed: right only if the sensor also says 0
        pct, acc = None, (100.0 if sensor == 0 else None)
    else:
        pct = round(100.0 * err / truth, 1)
        acc = round(max(0.0, 100.0 - abs(100.0 * err / truth)), 1)
    return {"sensor": sensor, "verified": truth, "error": err, "error_pct": pct,
            "accuracy_pct": acc}


def build_export(session: ReviewSession, clock_start: datetime | None,
                 sensor: SensorCounts | None = None) -> dict[str, Any]:
    verified = session.verified()
    counts = session.counts()
    progress = session.progress()
    cams: list[dict[str, Any]] = []
    warnings: list[str] = []
    left = progress["total"] - progress["done"]
    if left:
        warnings.append(f"The review is not finished: {left} of {progress['total']} items are "
                        f"still to review. Verified counts can only go up from here.")
    if session.drafts:
        warnings.append(f"{len(session.drafts)} item(s) have unfinished work (missed crossings "
                        f"added but no decision yet); those crossings are not counted.")

    for cam in session.cams:
        summ = cam.candidates["summary"]
        rows = [c for c in verified if c["camera"] == cam.sensor]
        n_in = sum(1 for c in rows if c["direction"] == "in")
        n_out = len(rows) - n_in
        rc = counts[cam.sensor]
        from_candidates = sum(1 for c in rows if c["source"] == "candidate")
        reviewed_props = rc["accepted"] + rc["rejected"] + rc["splits"]
        metrics = {
            "candidates_proposed": rc["candidates"],
            "candidates_reviewed": rc["candidates_reviewed"],
            "accepted": rc["accepted"],
            "rejected": rc["rejected"],
            "splits": rc["splits"],
            "direction_flipped": rc["direction_flipped"],
            "unsure": rc["unsure"],
            "unexplained_ranges": rc["unexplained_ranges"],
            "unexplained_reviewed": rc["unexplained_reviewed"],
            "unexplained_with_missed_crossing": rc["unexplained_with_missed_crossing"],
            "likely_missed_listed": summ.get("likely_missed_crossings", 0),
            "discards_reviewed": rc["discards_reviewed"],
            "discards_restored": rc["discards_restored"],
            "crossings_added_by_reviewer": rc["crossings_added"],
            "uturns_discarded": summ["discarded"].get("uturn_no_mask", 0),
            "returns_discarded": summ["discarded"].get("returned_same_track", 0),
            "pending_tracks_expired": summ["pending_expired"],
            "pending_expired_alarm": summ["pending_expired_alarm"],
            "stitched_tracks": summ["stitched_joins"],
            "track_splits": summ.get("track_splits", 0),
            "duplicates_merged": summ.get("duplicates", 0),
            "proposal_precision": (round((rc["accepted"] + rc["splits"]) / reviewed_props, 3)
                                   if reviewed_props else None),
            "proposal_recall": round(from_candidates / len(rows), 3) if rows else None,
        }
        try:
            cfg = load_config(cam.candidates["config"]["path"])
            rules: dict[str, Any] = {
                "rule": cfg.rule, "inside_side": cfg.inside_side,
                "min_dwell_in_zone_s": cfg.min_dwell_in_zone_s,
                "pending_timeout_s": cfg.pending_timeout_s,
                "stitch_gap_max_s": cfg.stitch_gap_max_s,
                "same_track_returns": cfg.same_track_returns,
                "mask_zone": [list(p) for p in cfg.mask_zone] if cfg.mask_zone else None,
                "filter_zones": len(cfg.filter_zones), "exclusion_zones": len(cfg.exclusion_zones),
                "site": cfg.site,
            }
        except (OSError, ValueError):
            rules = {"rule": cam.candidates["config"]["rule"], "site": cam.candidates["config"]["site"]}
        cam_sensor = (sensor.per_camera.get(cam.sensor) if sensor else None) or {}
        cams.append({
            "sensor": cam.sensor, "rules": rules, "in": n_in, "out": n_out, "rows": rows,
            "metrics": metrics,
            "comparison": {d: sensor_accuracy(cam_sensor.get(d), n_in if d == "in" else n_out)
                           for d in ("in", "out")} if cam_sensor else None,
        })
        if metrics["pending_expired_alarm"]:
            warnings.append(f"{cam.sensor}: many entries were lost before the mask zone "
                            f"(pending_expired {metrics['pending_tracks_expired']}); check the "
                            f"restored discards and missed crossings.")

    tot_in = sum(c["in"] for c in cams)
    tot_out = sum(c["out"] for c in cams)
    total_sensor: dict[str, int] = dict(sensor.total) if sensor and sensor.total else {}
    if sensor and sensor.per_camera and not sensor.total:  # add up per-camera numbers
        for d in ("in", "out"):
            vals = [sensor.per_camera.get(c.sensor, {}).get(d) for c in session.cams]
            if all(v is not None for v in vals):
                total_sensor[d] = sum(v for v in vals if v is not None)
    total_cmp = None
    if total_sensor:
        total_cmp = {d: sensor_accuracy(total_sensor.get(d), tot_in if d == "in" else tot_out)
                     for d in ("in", "out")}

    intervals: dict[str, dict[str, Any]] = {}
    if clock_start is not None:
        for c in verified:
            start = interval_start(clock_start + timedelta(seconds=c["t"]))
            key = start.strftime("%H:%M")
            b = intervals.setdefault(key, {"start": start.isoformat(), "in": 0, "out": 0,
                                           "per_camera": {}})
            b[c["direction"]] += 1
            pc = b["per_camera"].setdefault(c["camera"], {"in": 0, "out": 0})
            pc[c["direction"]] += 1

    tags: dict[str, int] = {}
    for c in verified:
        for t in c["tags"]:
            tags[t] = tags.get(t, 0) + 1

    first = session.cams[0].candidates
    return {
        "video": first["video"]["filename"], "operator": session.operator,
        "clock_start": clock_start.isoformat() if clock_start else None,
        "review": progress, "warnings": warnings, "cameras": cams,
        "total": {"in": tot_in, "out": tot_out}, "total_comparison": total_cmp,
        "intervals": dict(sorted(intervals.items())), "tags": tags,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def csv_text(export: dict[str, Any], cam: dict[str, Any]) -> str:
    """The manual tool's layout: a header block, a blank line, then the five columns."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    r = cam["rules"]
    w.writerow(["site", r.get("site", "")])
    w.writerow(["sensor", cam["sensor"]])
    w.writerow(["video", export["video"]])
    w.writerow(["video_start_clock", export["clock_start"] or ""])
    w.writerow(["operator", export["operator"]])
    w.writerow(["rule", r.get("rule", "")])
    for key in ("inside_side", "min_dwell_in_zone_s", "pending_timeout_s", "stitch_gap_max_s",
                "same_track_returns", "filter_zones", "exclusion_zones"):
        if key in r:
            w.writerow([key, r[key]])
    if r.get("mask_zone"):
        w.writerow(["mask_zone", json.dumps(r["mask_zone"])])
    if export["review"]["done"] < export["review"]["total"]:
        w.writerow(["review_incomplete",
                    f"{export['review']['total'] - export['review']['done']} items not reviewed"])
    w.writerow([])
    w.writerow(CSV_COLUMNS)
    start = datetime.fromisoformat(export["clock_start"]) if export["clock_start"] else None
    for c in cam["rows"]:
        clock = (start + timedelta(seconds=c["t"])).strftime("%H:%M:%S") if start else ""
        w.writerow([fmt_hms(c["t"]), f"{c['t']:.2f}", clock, c["direction"], ";".join(c["tags"])])
    return buf.getvalue()


def metrics_json(export: dict[str, Any]) -> dict[str, Any]:
    return {
        "video": export["video"], "operator": export["operator"], "review": export["review"],
        "cameras": {c["sensor"]: c["metrics"] for c in export["cameras"]},
        "warnings": export["warnings"],
    }


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v:+.1f}%"


def _sensor_cell(cmp_: dict[str, Any] | None, direction: str) -> str:
    value = ((cmp_ or {}).get(direction) or {}).get("sensor")
    return "–" if value is None else str(value)


def report_html(export: dict[str, Any]) -> str:
    e = html.escape
    rows = []
    for c in export["cameras"]:
        rows.append(
            f"<tr><td>{e(c['sensor'])}</td><td class=n>{c['in']}</td><td class=n>{c['out']}</td>"
            f"<td class=n>{_sensor_cell(c['comparison'], 'in')}</td>"
            f"<td class=n>{_sensor_cell(c['comparison'], 'out')}</td></tr>")
    tc = export["total_comparison"]
    total_row = (f"<tr class=total><td>All cameras</td><td class=n>{export['total']['in']}</td>"
                 f"<td class=n>{export['total']['out']}</td>"
                 f"<td class=n>{_sensor_cell(tc, 'in')}</td>"
                 f"<td class=n>{_sensor_cell(tc, 'out')}</td></tr>")

    acc_blocks = []
    for label, cmp_ in [("All cameras", export["total_comparison"])] + [
            (c["sensor"], c["comparison"]) for c in export["cameras"]]:
        if not cmp_:
            continue
        cells = "".join(
            f"<div class=stat><div class=k>{d.upper()}</div>"
            f"<div class=v>{'–' if x['accuracy_pct'] is None else str(x['accuracy_pct']) + '%'}</div>"
            f"<div class=s>sensor {x['sensor']} vs verified {x['verified']} "
            f"({_pct(x['error_pct'])})</div></div>"
            for d, x in cmp_.items() if x["sensor"] is not None)
        acc_blocks.append(f"<h3>{e(label)}</h3><div class=stats>{cells}</div>")

    interval_rows = "".join(
        f"<tr><td>{e(k)}</td><td class=n>{b['in']}</td><td class=n>{b['out']}</td></tr>"
        for k, b in export["intervals"].items())
    metric_keys = [
        ("candidates_proposed", "Proposed crossings"), ("accepted", "Accepted"),
        ("rejected", "Rejected"), ("splits", "Split into two"),
        ("direction_flipped", "Direction corrected"),
        ("proposal_precision", "Share of proposals that were real"),
        ("proposal_recall", "Share of real crossings the tool proposed"),
        ("unexplained_reviewed", "Unexplained stretches reviewed"),
        ("unexplained_with_missed_crossing", "…that contained a missed crossing"),
        ("discards_restored", "Rejections restored as real"),
        ("crossings_added_by_reviewer", "Crossings added by the reviewer"),
        ("uturns_discarded", "U-turns discarded"), ("returns_discarded", "Out-and-back discarded"),
        ("pending_tracks_expired", "Entries lost before the mask zone"),
        ("stitched_tracks", "Stitched tracks"), ("unsure", "Marked unsure"),
    ]
    mhead = "".join(f"<th>{e(c['sensor'])}</th>" for c in export["cameras"])
    mrows = "".join(
        "<tr><td>" + e(label) + "</td>" + "".join(
            f"<td class=n>{'–' if c['metrics'][k] is None else c['metrics'][k]}</td>"
            for c in export["cameras"]) + "</tr>" for k, label in metric_keys)
    warn = "".join(f"<li>{e(w)}</li>" for w in export["warnings"])
    tags = ", ".join(f"{e(k)} {v}" for k, v in sorted(export["tags"].items())) or "none"
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Count report · {e(export['video'])}</title>
<style>
:root {{ --bg:#fbfbfa; --text:#1d2127; --muted:#667080; --line:#e3e5e8; --accent:#2f6fde; --warn:#a15c00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171b; --text:#e7eaee; --muted:#97a1ad; --line:#2b333d; --accent:#6aa5ff; --warn:#f5b942; }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }}
main {{ max-width: 920px; margin: 0 auto; padding: 32px 20px 60px; }}
h1 {{ font-size: 24px; margin: 0 0 4px; }} h2 {{ font-size: 17px; margin: 32px 0 10px; }} h3 {{ font-size: 14px; margin: 16px 0 6px; color: var(--muted); }}
.muted {{ color: var(--muted); }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); }}
th {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; }}
td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; }} tr.total td {{ font-weight: 700; }}
.big {{ display:flex; gap: 28px; margin: 18px 0 6px; }} .big div {{ font-size: 13px; color: var(--muted); }} .big b {{ display:block; font-size: 40px; color: var(--text); line-height: 1.1; }}
.stats {{ display:flex; gap: 16px; flex-wrap: wrap; }} .stat {{ border:1px solid var(--line); border-radius: 8px; padding: 10px 14px; min-width: 200px; }}
.stat .k {{ font-size: 12px; color: var(--muted); }} .stat .v {{ font-size: 26px; font-weight: 700; }} .stat .s {{ font-size: 13px; color: var(--muted); }}
.warn {{ color: var(--warn); }}
</style></head><body><main>
<h1>Verified counts</h1>
<div class=muted>{e(export['video'])} · operator {e(export['operator'] or '–')} · video starts {e(export['clock_start'] or '–')} · review {export['review']['done']}/{export['review']['total']} items</div>
{f'<ul class=warn>{warn}</ul>' if warn else ''}
<div class=big><div><b>{export['total']['in']}</b>in</div><div><b>{export['total']['out']}</b>out</div></div>
<p class=muted>Counted by a person reviewing every proposal and every stretch of unexplained motion; the pipeline only suggests.</p>
<h2>Per camera</h2>
<table><tr><th>Camera</th><th class=n>Verified in</th><th class=n>Verified out</th><th class=n>Sensor in</th><th class=n>Sensor out</th></tr>
{''.join(rows)}{total_row}</table>
<h2>Sensor accuracy</h2>
{''.join(acc_blocks) or '<p class=muted>No sensor numbers were given. Re-run export.py with --sensor-in / --sensor-out (or --sensor CAMERA:in=N,out=M) to compare.</p>'}
<h2>15-minute intervals</h2>
{f'<table><tr><th>Starting</th><th class=n>In</th><th class=n>Out</th></tr>{interval_rows}</table>' if interval_rows else '<p class=muted>No clock time for video 00:00:00; pass --start to bucket by interval.</p>'}
<h2>Tags</h2><p>{tags}</p>
<h2>How the pipeline did</h2>
<p class=muted>Measured against the review. These are the tool's own error rates, needed before any claim about the sensor.</p>
<table><tr><th></th>{mhead}</tr>{mrows}</table>
<p class=muted>Exported {e(export['exported_at'])}.</p>
</main></body></html>
"""


def write_export(export: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for cam in export["cameras"]:
        p = out_dir / f"{cam['sensor']}.csv"
        p.write_text(csv_text(export, cam), encoding="utf-8")
        written.append(p)
    p = out_dir / "pipeline_metrics.json"
    write_json_atomic(p, metrics_json(export))
    written.append(p)
    p = out_dir / "report.html"
    p.write_text(report_html(export), encoding="utf-8")
    written.append(p)
    return written
