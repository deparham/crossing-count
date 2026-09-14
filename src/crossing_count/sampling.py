"""Which windows of a day a validation counts, and saying so on every result.

A validation measures the counting system during the footage that was counted, and
nothing more: how that footage was picked decides what the result may be said to
describe. So windows are picked by a stated rule, the rule is recorded with the
validation (the candidate windows, those chosen, the traffic level of each, the seed),
and every report says in plain words what its number describes
(docs/GROUND_TRUTH_SPECIFICATION.md, section 9):

    peak        the busiest windows for the traffic validated (the default: peak trading is
                what clients ask about, and where sensors fail), and with them at least one
                control window from a lower traffic level, drawn at random
    stratified  one window drawn at random from each traffic level the day has
    random      windows drawn at random from the trading hours

None is a better sample than another: they answer different questions. A peak result is a
valid measurement of peak trading; the control window is what tells crowding apart from a
sensor that is simply off.

Before counting, the only numbers there are the system's own, so a window's traffic level
at selection comes from them. The result's own level comes from the verified count, with
the same thresholds (validation.traffic_level).
"""

from __future__ import annotations

import random
import secrets
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from . import validation

SCHEMA = "sampling/1"
MODES = ("peak", "stratified", "random")
DEFAULT_MODE = "peak"
PEAK_WINDOWS = 3  # the busiest, and the next busiest that do not overlap it
RANDOM_WINDOWS = 3
LEVELS = tuple(validation.TRAFFIC_NAMES)  # quiet, normal, busy, heavy
CONTROL_PREFERENCE = ("normal", "quiet", "busy")  # a control shows typical trading first
LEVELS_FROM = "the system's own counts (before counting, the only numbers there are)"
DIRECTION_WORDS = {"in": "traffic in", "out": "traffic out", "both": "traffic in and out"}
BRIEF = ("schema", "mode", "role", "rank", "seed", "day", "length_min", "direction", "cameras",
         "level", "per_camera_hour")


class SamplingError(ValueError):
    """A sampling request that cannot be met, said plainly."""


def _overlaps(w: Mapping[str, Any], chosen: Sequence[Mapping[str, Any]]) -> bool:
    return any(w["from_min"] < c["until_min"] and w["until_min"] > c["from_min"] for c in chosen)


def levelled(windows: Sequence[Mapping[str, Any]], length_min: int,
             cameras: int) -> list[dict[str, Any]]:
    """Each window with its traffic per camera-hour (for the traffic validated) and level."""
    hours = length_min / 60 * max(1, cameras)
    out = []
    for w in windows:
        rate = float(w["score"]) / hours if hours else 0.0
        out.append({**w, "per_camera_hour": round(rate, 1),
                    "level": validation.traffic_level(rate)})
    return out


def _draw(rng: random.Random, pool: Sequence[dict[str, Any]],
          chosen: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """One window drawn at random from pool, clear of those chosen. Windows whose numbers the
    system marked complete are drawn from first: an imputed one is no fair comparison."""
    free = [w for w in pool if not _overlaps(w, chosen)]
    good = [w for w in free if w.get("validity") == "complete"] or free
    return rng.choice(good) if good else None


def _brief(w: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("start", "until", "in", "out", "score", "per_camera_hour", "level", "validity",
            "role", "rank")
    return {k: w[k] for k in keep if k in w}


def plan(windows: Sequence[Mapping[str, Any]], *, length_min: int, direction: str, cameras: int,
         mode: str = DEFAULT_MODE, seed: int | None = None, day: str | None = None
         ) -> dict[str, Any]:
    """The windows to validate, by the chosen rule, from every window of the day
    (retailnext.windows). The seed makes every random draw repeatable; without one, a new
    seed is drawn and recorded."""
    if mode not in MODES:
        raise SamplingError(f"The sampling is one of {', '.join(MODES)}, not {mode!r}.")
    seed = secrets.randbelow(1_000_000) if seed is None else int(seed)
    rng = random.Random(seed)
    cands = sorted(levelled(windows, length_min, cameras), key=lambda w: w["from_min"])
    chosen: list[dict[str, Any]] = []
    notes: list[str] = []
    if mode == "peak":
        for w in sorted(cands, key=lambda w: (-w["score"], w["from_min"])):
            if len(chosen) < PEAK_WINDOWS and not _overlaps(w, chosen):
                chosen.append({**w, "role": "peak", "rank": len(chosen) + 1})
        if chosen:
            top = LEVELS.index(chosen[0]["level"])
            for lv in (lv for lv in CONTROL_PREFERENCE if LEVELS.index(lv) < top):
                pick = _draw(rng, [w for w in cands if w["level"] == lv], chosen)
                if pick is not None:
                    chosen.append({**pick, "role": "control", "rank": 1})
                    break
            else:
                notes.append(
                    "No control window: the busiest time is already quiet traffic by the "
                    "system's own count, so there is no lower level to set it against."
                    if top == 0 else
                    "No control window: every window clear of the busiest is as busy as they are.")
    elif mode == "stratified":
        for lv in LEVELS:
            pick = _draw(rng, [w for w in cands if w["level"] == lv], chosen)
            if pick is not None:
                chosen.append({**pick, "role": "stratum", "rank": 1})
        if missing := [validation.TRAFFIC_NAMES[lv].lower() for lv in LEVELS
                       if all(w["level"] != lv for w in chosen)]:
            notes.append(f"The day has no window of {' or '.join(missing)} by the system's own "
                         f"count.")
    else:
        for k in range(RANDOM_WINDOWS):
            pick = _draw(rng, cands, chosen)
            if pick is None:
                break
            chosen.append({**pick, "role": "random", "rank": k + 1})
    return {"schema": SCHEMA, "mode": mode, "seed": seed, "day": day, "length_min": length_min,
            "direction": direction, "cameras": cameras, "levels_from": LEVELS_FROM,
            "thresholds": [limit for limit, _ in validation.TRAFFIC],
            "candidates": [_brief(w) for w in cands], "chosen": [_brief(w) for w in chosen],
            "notes": notes}


def record(p: Mapping[str, Any], start: str, until: str) -> dict[str, Any]:
    """What one validation keeps of the plan its footage came from: the whole plan, and
    which window this footage is (one not among those chosen was chosen by hand)."""
    at = (start, until)
    chosen = next((w for w in p["chosen"] if (w["start"], w["until"]) == at), None)
    window = chosen or next((w for w in p["candidates"] if (w["start"], w["until"]) == at),
                            {"start": start, "until": until})
    return {**{k: p[k] for k in ("schema", "mode", "seed", "day", "length_min", "direction",
                                 "cameras", "levels_from", "thresholds", "notes")},
            "role": chosen["role"] if chosen else "chosen by hand",
            "rank": chosen.get("rank") if chosen else None,
            "level": window.get("level"), "per_camera_hour": window.get("per_camera_hour"),
            "window": dict(window), "candidates": list(p["candidates"]),
            "chosen": list(p["chosen"])}


def brief(rec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A validation's sampling without the whole plan, for its result."""
    if not rec:
        return None
    w = rec.get("window") or {}
    return {**{k: rec.get(k) for k in BRIEF}, "window": {"start": w.get("start"),
                                                           "until": w.get("until")}}


def _day(text: Any) -> str:
    try:
        return date.fromisoformat(str(text)).strftime("%d/%m/%Y")
    except ValueError:
        return "the day"


def _ordinal(n: Any) -> str:
    n = int(n or 1)
    suffix = {2: "nd", 3: "rd"}.get(n, "th")
    return "" if n == 1 else f"{n}{suffix} "


def scope(rec: Mapping[str, Any] | None, period: str = "") -> str:
    """What the result describes, in plain words, for the report's first page."""
    if not rec:
        when = f" ({period})" if period else ""
        return ("Sampling: chosen by hand. This footage was not picked by a sampling rule, so the "
                f"result describes this period{when} only, not the store's trading in general.")
    day, length = _day(rec.get("day")), f"{rec.get('length_min')}-minute window"
    traffic = DIRECTION_WORDS.get(str(rec.get("direction")), "traffic")
    lv = validation.TRAFFIC_NAMES.get(str(rec.get("level")), "").lower()
    by = f" ({lv} by the system's own count)" if lv else ""
    seed, role = rec.get("seed"), rec.get("role")
    if role == "peak":
        return (f"Sampling: peak trading. This is the {_ordinal(rec.get('rank'))}busiest "
                f"{rec.get('length_min')} minutes of {day} for {traffic}{by}. The result is an "
                f"accuracy measurement of peak trading, not of the whole day.")
    if role == "control":
        return (f"Sampling: control window. A {length} of {lv}, drawn at random (seed {seed}) "
                f"clear of the busiest times of {day}, to set against the peak result. It "
                f"describes {lv.replace(' traffic', '')} trading, not peak trading.")
    if role == "stratum":
        return (f"Sampling: across traffic levels. A {length} of {lv}, drawn at random (seed "
                f"{seed}) from the {lv.replace(' traffic', '')} windows of {day}, one of a window "
                f"from each level. It describes {lv.replace(' traffic', '')} trading.")
    if role == "random":
        return (f"Sampling: at random. A {length} drawn at random (seed {seed}) from the trading "
                f"hours of {day}{by}. With others drawn the same way it describes the trading "
                f"day; on its own, this period.")
    return (f"Sampling: chosen by hand from the windows of {day}{by}, not by the sampling rule, "
            f"so the result describes this period only.")
