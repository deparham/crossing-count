"""Manual counting: a person watches each camera and presses I or O at every crossing.

Nothing here detects or tracks anyone. The tool plays the video, stores the time
of every key press, records which stretches of each camera were actually
watched, and lines the counts up against the sensor's own numbers for the same
15-minute intervals. State is saved on every change to
runs/<video>/manual/counts.json, so a count can be closed and resumed.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import layout as lay
from . import video as vid
from .export import CSV_COLUMNS, INTERVAL_MIN, interval_start, sensor_accuracy
from .util import fmt_hms, write_json_atomic

SCHEMA = "manual/1"
DIRECTIONS = ("in", "out")
MERGE_GAP_S = 0.5  # watched stretches closer than this are one stretch
MIN_GAP_S = 1.0  # unwatched stretches shorter than this are not reported
FULL_TOLERANCE_S = 5.0  # a video covering an interval to within this covers all of it
DEFAULT_NOTES = (
    "Count each person once, when they cross the light-blue counting line (the "
    "triangles point inside). Someone who crosses and comes back without going "
    "further is not counted. Where the camera has a grey mask band, an entry "
    "counts only if the person reaches it."
)


class ManualError(Exception):
    """A request the manual count cannot carry out."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _dur(seconds: float) -> str:
    s = max(0, round(seconds))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def merge_ranges(ranges: Iterable[Sequence[float]], gap: float = MERGE_GAP_S) -> list[list[float]]:
    """Union of [start, end] stretches; stretches closer than `gap` become one."""
    spans = sorted((min(float(r[0]), float(r[1])), max(float(r[0]), float(r[1]))) for r in ranges)
    out: list[list[float]] = []
    for a, b in spans:
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(a, 2), round(b, 2)] for a, b in out]


def unwatched_ranges(watched: Sequence[Sequence[float]], duration: float,
                     min_gap: float = MIN_GAP_S) -> list[list[float]]:
    """The stretches of [0, duration] not covered by `watched`."""
    gaps: list[list[float]] = []
    pos = 0.0
    for a, b in merge_ranges(watched):
        if a - pos >= min_gap:
            gaps.append([round(pos, 2), round(a, 2)])
        pos = max(pos, b)
    if duration - pos >= min_gap:
        gaps.append([round(pos, 2), round(duration, 2)])
    return gaps


def intervals_for(clock_start: datetime | None, duration: float) -> list[dict[str, Any]]:
    """The sensor's 15-minute intervals this video overlaps, and how much of each it covers."""
    if clock_start is None:
        return [{"key": "all", "label": "whole video", "start": None,
                 "covered_s": round(duration, 1), "full": None}]
    end = clock_start + timedelta(seconds=duration)
    step = timedelta(minutes=INTERVAL_MIN)
    out: list[dict[str, Any]] = []
    s = interval_start(clock_start)
    while s < end:
        e = s + step
        covered = (min(e, end) - max(s, clock_start)).total_seconds()
        out.append({"key": s.strftime("%H:%M"), "label": f"{s:%H:%M} - {e:%H:%M}",
                    "start": s.isoformat(), "covered_s": round(covered, 1),
                    "full": covered >= step.total_seconds() - FULL_TOLERANCE_S})
        s = e
    return out


def _compare(sensor: dict[str, int], you: dict[str, Any]) -> dict[str, Any] | None:
    if not sensor:
        return None
    return {d: sensor_accuracy(sensor.get(d), int(you[d])) for d in DIRECTIONS}


class ManualSession:
    """One video's manual count, saved to disk on every change."""

    def __init__(self, video: str | Path, run_dir: str | Path, operator: str = "",
                 start: datetime | None = None, camera_names: Sequence[str] | None = None) -> None:
        self.video_path = Path(video)
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "manual" / "counts.json"
        self._lock = threading.RLock()
        info = vid.probe(self.video_path)
        changed = False
        if self.path.is_file():
            self.state: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("fingerprint") != info.fingerprint:
                raise ManualError(f"{self.path} is the count of another video "
                                  f"({self.state.get('video')}); pick another --out-dir")
        else:
            self.state = self._new_state(info)
            changed = True
        if start is not None and self.state["clock_start"] != start.isoformat():
            self.state["clock_start"] = start.isoformat()
            changed = True
        if operator and operator != self.state["operator"]:
            self.state["operator"] = operator
            changed = True
        for cam, name in zip(self.state["cameras"], camera_names or []):
            if name.strip() and name.strip() != cam["name"]:
                cam["name"] = name.strip()
                changed = True
        if changed:
            self._save()

    # ---- setup -----------------------------------------------------------------------

    def _new_state(self, info: vid.VideoInfo) -> dict[str, Any]:
        audit = vid.audit_timebase(self.video_path)
        interval = vid.parse_filename_interval(info.filename)
        tiles, names = self._from_gate_layout(info.fingerprint)
        if not tiles:
            median = vid.median_of_video(self.video_path, audit.duration_s,
                                         interval_s=audit.median_interval_s)
            tiles = [t.as_dict() for t in lay.detect_tiles_in(median)]
        return {
            "schema": SCHEMA, "video": info.filename, "fingerprint": info.fingerprint,
            "duration_s": round(audit.duration_s, 3), "fps": round(audit.effective_fps, 3),
            "clock_start": interval.start if interval else None,
            "tz": interval.tz if interval else "",
            "timing_warnings": list(audit.warnings),
            "operator": "", "site": "", "notes": DEFAULT_NOTES,
            "cameras": [{"index": i, "name": names.get(i, f"Camera {i + 1}"), "tile": t}
                        for i, t in enumerate(tiles)],
            "counts": [], "next_id": 1, "watched": {}, "positions": {}, "sensor": {},
            "version": 0, "created_at": _now(),
        }

    def _from_gate_layout(self, fingerprint: str) -> tuple[list[dict[str, Any]], dict[int, str]]:
        """Pictures and camera names from gate.py's layout.json for this video, if any."""
        try:
            layout = json.loads((self.run_dir / "layout.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return [], {}
        if layout.get("fingerprint") != fingerprint:
            return [], {}
        tiles = [dict(p) for p in layout.get("pictures", [])]
        names = {int(c["picture"]): str(c["sensor"]) for c in layout.get("cameras", [])
                 if isinstance(c.get("picture"), int) and c.get("sensor")}
        return tiles, names

    def _save(self) -> None:
        self.state["version"] = int(self.state.get("version", 0)) + 1
        self.state["updated_at"] = _now()
        write_json_atomic(self.path, self.state)

    def _camera(self, camera: int) -> dict[str, Any]:
        cams = self.state["cameras"]
        if not 0 <= camera < len(cams):
            raise ManualError(f"there is no camera {camera + 1}")
        return dict(cams[camera])

    def clock_start(self) -> datetime | None:
        cs = self.state["clock_start"]
        return datetime.fromisoformat(cs) if cs else None

    # ---- changes (each saved at once) --------------------------------------------------

    def add(self, camera: int, t: float, direction: str) -> dict[str, Any]:
        with self._lock:
            self._camera(camera)
            if direction not in DIRECTIONS:
                raise ManualError(f"direction must be in or out, not {direction!r}")
            if not 0.0 <= t <= self.state["duration_s"] + 1.0:
                raise ManualError(f"{t:.2f} s is outside the video")
            c = {"id": self.state["next_id"], "camera": camera, "t": round(float(t), 2),
                 "direction": direction, "at": _now()}
            self.state["next_id"] += 1
            self.state["counts"].append(c)
            self._save()
            return c

    def delete(self, count_id: int) -> bool:
        with self._lock:
            before = len(self.state["counts"])
            self.state["counts"] = [c for c in self.state["counts"] if c["id"] != count_id]
            if len(self.state["counts"]) == before:
                return False
            self._save()
            return True

    def undo(self, camera: int) -> dict[str, Any] | None:
        """Remove the count most recently added on this camera."""
        with self._lock:
            mine = [c for c in self.state["counts"] if c["camera"] == camera]
            if not mine:
                return None
            last = max(mine, key=lambda c: c["id"])
            self.delete(last["id"])
            return dict(last)

    def watched(self, camera: int, start: float, end: float) -> None:
        with self._lock:
            self._camera(camera)
            dur = self.state["duration_s"]
            a, b = max(0.0, min(start, end)), min(dur, max(start, end))
            key = str(camera)
            if b > a:
                self.state["watched"][key] = merge_ranges(
                    [*self.state["watched"].get(key, []), [a, b]])
            self.state["positions"][key] = round(min(dur, max(0.0, end)), 2)
            self._save()

    def set_position(self, camera: int, t: float) -> None:
        with self._lock:
            self._camera(camera)
            self.state["positions"][str(camera)] = round(min(self.state["duration_s"],
                                                             max(0.0, t)), 2)
            self._save()

    def rename_camera(self, camera: int, name: str) -> None:
        with self._lock:
            self._camera(camera)
            if not name.strip():
                raise ManualError("a camera needs a name")
            self.state["cameras"][camera]["name"] = name.strip()[:80]
            self._save()

    def set_meta(self, operator: str | None = None, site: str | None = None,
                 notes: str | None = None) -> None:
        with self._lock:
            for key, value in (("operator", operator), ("site", site), ("notes", notes)):
                if value is not None:
                    self.state[key] = value.strip()
            self._save()

    def set_sensor(self, interval: str, camera: int | None, direction: str,
                   value: int | None) -> None:
        """The sensor's number for one interval, one camera (None: all cameras), one direction."""
        with self._lock:
            keys = {iv["key"] for iv in intervals_for(self.clock_start(), self.state["duration_s"])}
            if interval not in keys:
                raise ManualError(f"the video does not cover the interval {interval}")
            if direction not in DIRECTIONS:
                raise ManualError(f"direction must be in or out, not {direction!r}")
            if value is not None and value < 0:
                raise ManualError("a count cannot be negative")
            who = "all" if camera is None else str(camera)
            if camera is not None:
                self._camera(camera)
            entry = self.state["sensor"].setdefault(interval, {}).setdefault(who, {})
            if value is None:
                entry.pop(direction, None)
            else:
                entry[direction] = int(value)
            self._save()

    # ---- reading -----------------------------------------------------------------------

    def _key(self, clock: datetime | None, t: float) -> str:
        if clock is None:
            return "all"
        t = min(max(t, 0.0), max(0.0, self.state["duration_s"] - 1e-3))
        return interval_start(clock + timedelta(seconds=t)).strftime("%H:%M")

    def _clock_text(self, t: float) -> str:
        clock = self.clock_start()
        return (clock + timedelta(seconds=t)).strftime("%H:%M:%S") if clock else fmt_hms(t)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            st = self.state
            dur = float(st["duration_s"])
            clock = self.clock_start()
            cams: list[dict[str, Any]] = []
            per: dict[tuple[str, int], dict[str, int]] = {}
            for c in st["counts"]:
                bucket = per.setdefault((self._key(clock, c["t"]), c["camera"]), {"in": 0, "out": 0})
                bucket[c["direction"]] += 1
            for cam in st["cameras"]:
                i = cam["index"]
                mine = [c for c in st["counts"] if c["camera"] == i]
                w = st["watched"].get(str(i), [])
                ws = sum(b - a for a, b in w)
                cams.append({
                    "index": i, "name": cam["name"],
                    "in": sum(1 for c in mine if c["direction"] == "in"),
                    "out": sum(1 for c in mine if c["direction"] == "out"),
                    "watched": w, "watched_s": round(ws, 1),
                    "watched_pct": round(min(100.0, 100.0 * ws / dur), 1) if dur else 0.0,
                    "unwatched": unwatched_ranges(w, dur),
                })
            intervals: list[dict[str, Any]] = []
            for iv in intervals_for(clock, dur):
                entered = st["sensor"].get(iv["key"], {})
                rows = []
                for cam in cams:
                    you = per.get((iv["key"], cam["index"]), {"in": 0, "out": 0})
                    sensor = dict(entered.get(str(cam["index"]), {}))
                    rows.append({"index": cam["index"], "name": cam["name"], **you,
                                 "sensor": sensor, "comparison": _compare(sensor, you)})
                all_you = {d: sum(r[d] for r in rows) for d in DIRECTIONS}
                all_sensor = dict(entered.get("all", {}))
                source = {d: "entered" for d in all_sensor}
                for d in DIRECTIONS:
                    if d not in all_sensor and rows and all(d in r["sensor"] for r in rows):
                        all_sensor[d] = sum(r["sensor"][d] for r in rows)
                        source[d] = "sum of cameras"
                intervals.append({**iv, "cameras": rows,
                                  "all": {**all_you, "sensor": all_sensor, "sensor_source": source,
                                          "comparison": _compare(all_sensor, all_you)}})
            warnings = list(st.get("timing_warnings", []))
            if clock is None:
                warnings.append("No clock time for the start of the video, so counts cannot be "
                                "put into the sensor's 15-minute intervals. Restart with "
                                "--start \"YYYY-MM-DD HH:MM:SS\".")
            for iv in intervals:
                if iv["full"] is False:
                    warnings.append(f"{iv['label']}: the video covers only {_dur(iv['covered_s'])} "
                                    f"of this interval, so the sensor's figure for the whole "
                                    f"interval is not comparable.")
            for cam in cams:
                gaps = cam["unwatched"]
                if gaps:
                    missing = sum(b - a for a, b in gaps)
                    where = ", ".join(f"{self._clock_text(a)}-{self._clock_text(b)}"
                                      for a, b in gaps[:4]) + (" ..." if len(gaps) > 4 else "")
                    warnings.append(f"{cam['name']}: {_dur(missing)} not watched yet ({where}); "
                                    f"counts there are missing.")
            return {"cameras": cams, "intervals": intervals, "warnings": warnings,
                    "total": {d: sum(c[d] for c in cams) for d in DIRECTIONS}}

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {**self.state, "summary": self.summary()}

    # ---- export ------------------------------------------------------------------------

    def csv_text(self, camera: int) -> str:
        """The manual tool's layout: a header block, a blank line, then the five columns."""
        with self._lock:
            st, cam = self.state, self._camera(camera)
            s = self.summary()["cameras"][camera]
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(["site", st["site"]])
            w.writerow(["sensor", cam["name"]])
            w.writerow(["video", st["video"]])
            w.writerow(["video_start_clock", st["clock_start"] or ""])
            w.writerow(["operator", st["operator"]])
            w.writerow(["rule", "manual count"])
            w.writerow(["counting_rules", st["notes"]])
            w.writerow(["watched_pct", s["watched_pct"]])
            if s["unwatched"]:
                w.writerow(["not_watched", "; ".join(f"{fmt_hms(a)}-{fmt_hms(b)}"
                                                     for a, b in s["unwatched"])])
            w.writerow([])
            w.writerow(CSV_COLUMNS)
            for c in sorted((c for c in st["counts"] if c["camera"] == camera),
                            key=lambda c: (c["t"], c["id"])):
                clock = self._clock_text(c["t"]) if st["clock_start"] else ""
                w.writerow([fmt_hms(c["t"]), f"{c['t']:.2f}", clock, c["direction"], ""])
            return buf.getvalue()

    def report_html(self) -> str:
        e = html.escape
        with self._lock:
            st = self.state
            s = self.summary()

        def acc_cells(x: dict[str, Any]) -> str:
            if x["sensor"] is None:
                return "<td class=n>–</td><td class=n>–</td>"
            acc = "–" if x["accuracy_pct"] is None else f"{x['accuracy_pct']}%"
            return (f"<td class=n>{x['sensor']}</td>"
                    f"<td class=n>{acc} <span class=muted>({x['error']:+d})</span></td>")

        def row(name: str, r: dict[str, Any], total: bool = False) -> str:
            cmp_ = r["comparison"] or {d: sensor_accuracy(None, r[d]) for d in DIRECTIONS}
            return (f"<tr{' class=total' if total else ''}><td>{e(name)}</td>"
                    f"<td class=n>{r['in']}</td>{acc_cells(cmp_['in'])}"
                    f"<td class=n>{r['out']}</td>{acc_cells(cmp_['out'])}</tr>")

        head = ("<tr><th>Camera</th><th class=n>You in</th><th class=n>Sensor in</th>"
                "<th class=n>In accuracy</th><th class=n>You out</th><th class=n>Sensor out</th>"
                "<th class=n>Out accuracy</th></tr>")
        blocks = []
        for iv in s["intervals"]:
            part = ("" if iv["full"] is not False else
                    f" <span class=warn>(the video covers {_dur(iv['covered_s'])} of it)</span>")
            body = "".join(row(c["name"], c) for c in iv["cameras"])
            if len(iv["cameras"]) > 1:
                body += row("All cameras", iv["all"], total=True)
            blocks.append(f"<h3>{e(iv['label'])}{part}</h3><table>{head}{body}</table>")
        cam_rows = "".join(
            f"<tr><td>{e(c['name'])}</td><td class=n>{c['in']}</td><td class=n>{c['out']}</td>"
            f"<td class=n>{c['watched_pct']}%</td></tr>" for c in s["cameras"])
        warn = "".join(f"<li>{e(x)}</li>" for x in s["warnings"])
        return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Manual count · {e(st['video'])}</title>
<style>
:root {{ --bg:#fbfbfa; --text:#1d2127; --muted:#667080; --line:#e3e5e8; --warn:#a15c00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171b; --text:#e7eaee; --muted:#97a1ad; --line:#2b333d; --warn:#f5b942; }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }}
main {{ max-width: 960px; margin: 0 auto; padding: 32px 20px 60px; }}
h1 {{ font-size: 24px; margin: 0 0 4px; }} h2 {{ font-size: 17px; margin: 32px 0 10px; }} h3 {{ font-size: 14px; margin: 18px 0 6px; }}
.muted {{ color: var(--muted); }} .warn {{ color: var(--warn); }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); }}
th {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; }}
td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; }} tr.total td {{ font-weight: 700; }}
.big {{ display:flex; gap: 28px; margin: 18px 0 6px; }} .big div {{ font-size: 13px; color: var(--muted); }} .big b {{ display:block; font-size: 40px; color: var(--text); line-height: 1.1; }}
</style></head><body><main>
<h1>Manual count</h1>
<div class=muted>{e(st['video'])} · site {e(st['site'] or '–')} · operator {e(st['operator'] or '–')} · video starts {e(st['clock_start'] or '–')} {e(st['tz'])}</div>
{f'<ul class=warn>{warn}</ul>' if warn else ''}
<div class=big><div><b>{s['total']['in']}</b>in</div><div><b>{s['total']['out']}</b>out</div></div>
<p class=muted>Counted by a person watching each camera and pressing a key at every crossing of the counting line. No automatic detection was used.</p>
<h2>Per camera</h2>
<table><tr><th>Camera</th><th class=n>In</th><th class=n>Out</th><th class=n>Watched</th></tr>{cam_rows}</table>
<h2>Against the sensor, per 15-minute interval</h2>
{''.join(blocks)}
<p class=muted>Accuracy = 100% minus the sensor's error as a share of your count; the number in brackets is the sensor minus you.</p>
<h2>Counting rules followed</h2>
<p>{e(st['notes'] or '–')}</p>
<p class=muted>Exported {e(_now())}.</p>
</main></body></html>
"""

    def export(self, out_dir: Path | None = None) -> list[Path]:
        """Write one CSV per camera, summary.json and report.html."""
        out = out_dir or self.run_dir / "manual" / "export"
        out.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        with self._lock:
            used: set[str] = set()
            for cam in self.state["cameras"]:
                stem = re.sub(r"[^A-Za-z0-9._-]+", "_", cam["name"]).strip("_") \
                    or f"camera_{cam['index'] + 1}"
                while stem in used:
                    stem += f"_{cam['index'] + 1}"
                used.add(stem)
                p = out / f"{stem}.csv"
                p.write_text(self.csv_text(cam["index"]), encoding="utf-8")
                written.append(p)
            st = self.state
            p = out / "summary.json"
            write_json_atomic(p, {
                "video": st["video"], "fingerprint": st["fingerprint"], "site": st["site"],
                "operator": st["operator"], "clock_start": st["clock_start"], "tz": st["tz"],
                "counting_rules": st["notes"], "exported_at": _now(),
                "summary": self.summary(), "counts": st["counts"]})
            written.append(p)
            p = out / "report.html"
            p.write_text(self.report_html(), encoding="utf-8")
            written.append(p)
        return written
