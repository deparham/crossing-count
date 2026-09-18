# Sensor data: getting a counting system's numbers in

The validation engine compares verified counts with a counting system's numbers in one
normalised form, whatever the system: interval counts with a start and end on the
store's own clock, a direction, a count, the camera (or all cameras together) and whether
the source says the number is complete (`sensors.IntervalCount`).

## RetailNext

**Fetch from RetailNext** on the wizard's system step asks RetailNext's API for each
camera's 15-minute counts over the footage's period (or the store's total, when its
entrances are not named like the cameras). The API key is in the credential store; see the
README.

**The footage and the intervals.** A counting system reports whole 15-minute intervals,
and an export rarely matches them to the second, so:

- An interval the footage only brushes (under 5 seconds of it, as when an export runs a
  fraction of a second past the quarter hour) is not compared at all. It would otherwise
  need a number for fifteen minutes nobody counted, and the fetch would fail for want of
  it.
- Footage shorter than the intervals it is compared with by more than 30 seconds is said
  so: a validation check ("Footage covers the period RetailNext's numbers describe", with
  the minutes and the share) and a line on the report's first page. The system's number
  then covers minutes that were never counted, so the difference is not like for like.
  The numbers are never scaled to fit: that would invent counts.

## Any other counter: a CSV file

**Import a CSV** on the same step. One row per interval:

```
camera,start,end,in,out
CN-123-PB1,2026-09-12 11:30,2026-09-12 11:45,15,13
CN-123-R2,2026-09-12 11:30,2026-09-12 11:45,5,10
```

or, one row per direction:

```
start,end,direction,count
2026-09-12 11:30,2026-09-12 11:45,in,20
2026-09-12 11:30,2026-09-12 11:45,out,23
```

| Column | Required | Meaning |
|---|---|---|
| `start`, `end` | yes | the interval, on the store's local clock: `2026-09-12 11:30`, `2026-09-12T11:30:00` or `12/09/2026 11:30` (day first). A time zone offset is dropped: give local time. |
| `in`, `out` | one of the two forms | the counts per direction (either or both columns) |
| `direction`, `count` | one of the two forms | one row per direction |
| `camera` | no | the camera or sensor, named as in CrossingCount; empty = all the footage's cameras together |
| `validity` | no | `complete`, or anything else (e.g. `imputed`) to say the number is not a full count: it is shown as a warning |
| `system` | no | the system's name for reports (e.g. `Xovis`); otherwise the file's name |

Rows may be finer than 15 minutes (they are added up); every 15-minute interval of the
footage must be covered exactly once, or the import says which interval is missing or
counted twice. Headers are matched without regard to case; a spreadsheet's byte-order mark
is ignored.

## Adding another system

A new source is one class with `fetch(cameras, start, end) -> SensorData` in `sensors.py`.
Nothing else changes: the wizard and the engine only ever see `SensorData`.
