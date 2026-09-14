"""Counting systems' numbers, in the one form the validation engine reads.

The engine never sees a vendor's format. Each adapter turns one source into interval
counts: a start and end on the store's own clock, a direction, a count, the camera ("" for
all the footage's cameras together) and whether the source says the number is complete.

    RetailNextAdapter   RetailNext's API (retailnext.py does the talking)
    CsvAdapter          a CSV file, from any counter that can export one (Xovis, V-Count,
                        FootfallCam, a spreadsheet): see docs/SENSOR_DATA.md

Anything new (another vendor's API) is one more class with the same fetch().
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from . import retailnext as rn

DIRS = ("in", "out")
MAX_ROWS = 200_000


class SensorError(Exception):
    """Numbers that cannot be read, said plainly (with the CSV line when there is one)."""


@dataclass(frozen=True)
class IntervalCount:
    start: datetime  # the store's own clock, no time zone
    end: datetime
    direction: str  # "in" or "out"
    count: int
    camera: str = ""  # "" = all the footage's cameras together
    validity: str = "complete"  # anything else: the source says it is not a full count


@dataclass
class SensorData:
    system: str  # the system under test, as reports name it ("RetailNext", "Xovis")
    source: str  # where these numbers came from ("RetailNext API", "CSV file")
    counts: list[IntervalCount]
    detail: dict[str, Any] = field(default_factory=dict)  # store, subscription, file, cameras
    notes: list[str] = field(default_factory=list)


class SensorAdapter(Protocol):
    def fetch(self, cameras: list[str], start: datetime, end: datetime) -> SensorData:
        """The system's numbers for these cameras over [start, end)."""
        ...


# ---- RetailNext ----------------------------------------------------------------------------

def _at(day: date, hhmm: str) -> datetime:
    """"HH:MM" on a day, on the store's own clock ("24:00" is the next midnight)."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.fromisoformat(day.isoformat()) + timedelta(hours=h, minutes=m)


def from_retailnext(got: dict[str, Any], day: date | None = None) -> SensorData:
    """retailnext.camera_counts' answer as interval counts: each camera's rows, or the
    store's total when its entrances are not named like the cameras. The day is the
    answer's own, or the footage's when the answer does not say."""
    if got.get("day"):
        day = date.fromisoformat(str(got["day"]))
    if day is None:
        raise SensorError("RetailNext's answer does not say which day it is for.")
    counts = []
    sources = got.get("cameras") or {"": got.get("total") or []}
    for cam, rows in sources.items():
        for r in rows:
            for d in DIRS:
                if r.get(d) is not None:
                    counts.append(IntervalCount(_at(day, str(r["start"])), _at(day, str(r["finish"])),
                                                d, int(r[d]), cam, str(r.get("validity") or "complete")))
    return SensorData("RetailNext", "RetailNext API", counts,
                      {"subscription": got.get("subscription"), "store": got.get("store"),
                       "cameras": list(got.get("cameras") or {}) or ["the store's total"]},
                      [str(got["note"])] if got.get("note") else [])


class RetailNextAdapter:
    def __init__(self, conn: rn.Connection, nodes: list[dict[str, Any]], code: str) -> None:
        self.conn, self.nodes, self.code = conn, nodes, code

    def fetch(self, cameras: list[str], start: datetime, end: datetime) -> SensorData:
        got = rn.camera_counts(self.conn, self.nodes, self.code, cameras, start, end)
        got["subscription"] = self.conn.subscription
        return from_retailnext(got)


# ---- CSV -----------------------------------------------------------------------------------

_FORMATS = ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M")


def _when(text: str, line: int, what: str) -> datetime:
    t = text.strip()
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        for f in _FORMATS:
            try:
                return datetime.strptime(t, f)  # noqa: DTZ007  (the store's own clock, as the footage's)
            except ValueError:
                continue
        raise SensorError(f"Line {line}: {what} {t!r} is not a date and time (use "
                          f"2026-09-12 11:30).") from None
    return d.replace(tzinfo=None)  # the store's own clock, as the footage's


def read_csv(text: str, name: str = "numbers.csv") -> SensorData:
    """A counter's numbers from CSV, one row per interval: columns start, end, and either
    in and/or out, or direction and count; camera, validity and system optional."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise SensorError("The file is empty.")
    cols = {f.strip().lower(): f for f in reader.fieldnames if f}
    missing = [c for c in ("start", "end") if c not in cols]
    wide = [d for d in DIRS if d in cols]
    if missing or not (wide or {"direction", "count"} <= cols.keys()):
        raise SensorError("The file needs columns start and end, and either in and/or out, or "
                          "direction and count (camera, validity and system are optional).")

    def get(row: dict[str, str | None], col: str) -> str:
        return str(row.get(cols[col]) or "").strip() if col in cols else ""

    counts: list[IntervalCount] = []
    system = ""
    for k, row in enumerate(reader, start=2):
        if k > MAX_ROWS:
            raise SensorError(f"More than {MAX_ROWS} rows: split the file.")
        if not any(str(v or "").strip() for v in row.values()):
            continue
        start, end = _when(get(row, "start"), k, "start"), _when(get(row, "end"), k, "end")
        if end <= start:
            raise SensorError(f"Line {k}: the end is not after the start.")
        pairs = ([(d, get(row, d)) for d in wide] if wide
                 else [(get(row, "direction").lower(), get(row, "count"))])
        for d, value in pairs:
            if d not in DIRS:
                raise SensorError(f"Line {k}: direction {d!r} is not in or out.")
            if value == "":
                continue
            try:
                n = int(float(value))
            except ValueError:
                raise SensorError(f"Line {k}: {value!r} is not a count.") from None
            if n < 0 or n != float(value):
                raise SensorError(f"Line {k}: a count is a whole number, 0 or more.")
            counts.append(IntervalCount(start, end, d, n, get(row, "camera"),
                                        get(row, "validity") or "complete"))
        system = system or get(row, "system")
    if not counts:
        raise SensorError("The file has no counts.")
    return SensorData(system or Path(name).stem, "CSV file", counts,
                      {"file": name, "cameras": sorted({c.camera for c in counts if c.camera})
                       or ["all cameras together"]})


class CsvAdapter:
    def __init__(self, text: str, name: str = "numbers.csv") -> None:
        self.data = read_csv(text, name)

    def fetch(self, cameras: list[str], start: datetime, end: datetime) -> SensorData:
        names = {c.lower() for c in cameras}
        keep = [c for c in self.data.counts
                if c.end > start and c.start < end and (not c.camera or c.camera.lower() in names)]
        if not keep:
            raise SensorError(f"{self.data.detail['file']} has no numbers for these cameras "
                              f"between {start:%d/%m/%Y %H:%M} and {end:%H:%M}.")
        return SensorData(self.data.system, self.data.source, keep, dict(self.data.detail),
                          list(self.data.notes))
