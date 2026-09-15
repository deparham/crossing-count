"""The validation engine for counts: how far a counting system's numbers are from the
verified counts, interval by interval.

Most people counters (RetailNext among them) give a count per interval, not the
individual crossings, so a system under test is judged by its count error. The engine
reads normalised rows only, never a vendor's format (sensors.py does the translating):

    {"interval": label, "direction": "in" | "out", "truth": verified crossings,
     "system": the system's count, and optionally "validation", "covered_s", "cameras"}

and gives, per direction and for both directions together (docs/METRICS_SPECIFICATION.md):

    error e = system - truth, per interval (positive: the system counted too many)
    total error E = sum of e             bias % = 100 E / sum of truth
    overcount = sum of the positive e     undercount = sum of -e where e is negative
    MAE = mean |e|                        RMSE = sqrt(mean e^2)
    WAPE % = 100 sum |e| / sum of truth    (over- and undercounts do not cancel out)
    MAPE % = mean of 100 |e| / truth, over intervals with truth >= MIN_TRUTH_FOR_PCT only

Across validations, uncertainty comes from resampling whole validations (or whole
stores) with replacement: the intervals of one clip are not independent (same camera,
same day, same people), so resampling them one by one would claim more certainty than
there is. With fewer than MIN_CLUSTERS validations (or stores), no range is given.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ENGINE = "count-validation/1.0"
MIN_TRUTH_FOR_PCT = 10  # an interval with fewer verified crossings gives no percentage error
MIN_VERIFIED_FOR_PCT = 30  # fewer: the counts and the difference are said, no percentage (quote)
MIN_CLUSTERS = 5
BOOTSTRAP_N = 2000
SEED = 20260914  # fixed: the same data always gives the same range
DIRS = ("in", "out")
TOTAL = "total"
TRAFFIC = ((40.0, "quiet"), (120.0, "normal"), (240.0, "busy"))  # crossings per camera-hour
TRAFFIC_NAMES = {"quiet": "Quiet traffic", "normal": "Normal traffic", "busy": "Busy traffic",
                 "heavy": "Heavy traffic"}

Row = Mapping[str, Any]


def traffic_level(per_camera_hour: float) -> str:
    """Below 40 crossings per camera-hour quiet, below 120 normal, below 240 busy, else heavy
    (provisional thresholds, stored with every result that uses them)."""
    return next((name for limit, name in TRAFFIC if per_camera_hour < limit), "heavy")


def _pct(x: float, of: float) -> float | None:
    return round(100.0 * x / of, 1) if of else None


def quote(truth: int, system: int | None) -> dict[str, Any]:
    """What may be said of one comparison. A percentage only on MIN_VERIFIED_FOR_PCT or more
    verified crossings: on fewer, one crossing moves it by more than three points and the
    figure says more about the sample than about the system. Otherwise the counts and the
    signed difference, in words: "verified 11, the system counted 9: an undercount of 2"."""
    if system is None:
        return {"truth": truth, "system": None, "error": None, "rate": False,
                "words": "no number from the system", "reason": None}
    err = int(system) - int(truth)
    words = (f"an undercount of {-err}" if err < 0 else f"an overcount of {err}" if err > 0
             else "no difference")
    rate = truth >= MIN_VERIFIED_FOR_PCT
    reason = None if rate else (
        f"{truth} verified crossing{'s' if truth != 1 else ''}: fewer than "
        f"{MIN_VERIFIED_FOR_PCT} are too few for a percentage"
        + (f" (one crossing moves it by {100 / truth:.0f} points)" if truth else ""))
    return {"truth": truth, "system": int(system), "error": err, "rate": rate, "words": words,
            "reason": reason}


def metrics(rows: Sequence[Row]) -> dict[str, Any]:
    """Count error measures over some intervals (one direction, or both summed)."""
    errs = [int(r["system"]) - int(r["truth"]) for r in rows]
    truth = sum(int(r["truth"]) for r in rows)
    n = len(errs)
    big = [r for r in rows if int(r["truth"]) >= MIN_TRUTH_FOR_PCT]
    return {
        "intervals": n, "truth": truth, "system": sum(int(r["system"]) for r in rows),
        "error": sum(errs), "bias_pct": _pct(sum(errs), truth),
        "overcount": sum(e for e in errs if e > 0), "undercount": -sum(e for e in errs if e < 0),
        "mae": round(sum(abs(e) for e in errs) / n, 2) if n else None,
        "rmse": round(math.sqrt(sum(e * e for e in errs) / n), 2) if n else None,
        "wape_pct": _pct(sum(abs(e) for e in errs), truth),
        "mape_pct": round(sum(100.0 * abs(int(r["system"]) - int(r["truth"])) / int(r["truth"])
                              for r in big) / len(big), 1) if big else None,
        "mape_intervals": len(big),
        "max_abs_error": max((abs(e) for e in errs), default=None),
    }


def select(rows: Sequence[Row], direction: str) -> list[dict[str, Any]]:
    """One direction's rows, or (TOTAL) each interval's in and out summed."""
    if direction != TOTAL:
        return [dict(r) for r in rows if r["direction"] == direction]
    together: dict[tuple[Any, Any], dict[str, Any]] = {}
    for r in rows:
        key = (r.get("validation"), r["interval"])
        acc = together.setdefault(key, {**{k: v for k, v in r.items()
                                           if k not in ("direction", "truth", "system")},
                                        "direction": TOTAL, "truth": 0, "system": 0})
        acc["truth"] += int(r["truth"])
        acc["system"] += int(r["system"])
    return list(together.values())


def by_direction(rows: Sequence[Row]) -> dict[str, dict[str, Any]]:
    """Measures for each direction present, and for both together."""
    dirs = [d for d in DIRS if any(r["direction"] == d for r in rows)]
    out = {d: metrics(select(rows, d)) for d in dirs}
    if len(dirs) > 1:
        out[TOTAL] = metrics(select(rows, TOTAL))
    return out


def bootstrap(clusters: Sequence[Sequence[Row]], stat: Callable[[list[Row]], float | None],
              n: int = BOOTSTRAP_N, seed: int = SEED) -> list[float] | None:
    """The 95% percentile range of stat over resamples of whole clusters, or None with too
    few clusters."""
    if len(clusters) < MIN_CLUSTERS:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(n):
        sample = [row for _ in clusters for row in clusters[rng.randrange(len(clusters))]]
        v = stat(sample)
        if v is not None:
            values.append(v)
    if len(values) < 0.9 * n:
        return None
    values.sort()
    return [round(values[int(0.025 * (len(values) - 1))], 1),
            round(values[int(0.975 * (len(values) - 1))], 1)]


def row_level(r: Row) -> str | None:
    """An interval's traffic level, from its own verified crossings per camera-hour."""
    hours = float(r.get("covered_s") or 0) / 3600 * max(1, int(r.get("cameras") or 1))
    return traffic_level(int(r["truth"]) / hours) if hours else None


def by_level(rows: Sequence[Row]) -> dict[str, dict[str, Any]]:
    """Measures per traffic level, each interval (the directions validated, together) placed
    by its own verified crossings per camera-hour. A system's error in a crowd is not its
    error in a quiet hour: this is where the difference shows, and what tells crowding apart
    from a system that is simply off."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in select(rows, TOTAL):
        if (lv := row_level(r)) is not None:
            groups.setdefault(lv, []).append(r)
    order = list(TRAFFIC_NAMES)
    return {TRAFFIC_NAMES[k]: {**metrics(rs), "validations": len({r.get("validation") for r in rs})}
            for k, rs in sorted(groups.items(), key=lambda kv: order.index(kv[0]))}


def level_sentence(levels: Mapping[str, Mapping[str, Any]], system: str) -> str | None:
    """The error at each traffic level, in words: "At busy traffic (4 intervals, 380 verified
    crossings) RetailNext counted 6.0% too few; at normal traffic (...) 0.5% too many."."""
    parts = []
    for name, m in levels.items():
        bias = m.get("bias_pct")
        if bias is None:
            continue
        n, err = int(m["intervals"]), int(m["error"])
        if int(m["truth"]) < MIN_VERIFIED_FOR_PCT:  # too few for a percentage: the difference
            way = (f"{-err} fewer" if err < 0 else f"{err} more" if err > 0 else "exactly as many")
            way += " (too few crossings for a percentage)" if err else ""
        else:
            way = (f"{abs(bias):.1f}% too many" if bias > 0 else f"{abs(bias):.1f}% too few"
                   if bias < 0 else "exactly as many")
        parts.append(f"at {name.lower()} ({n} interval{'s' if n != 1 else ''}, {m['truth']} "
                     f"verified crossings) {system} counted {way}")
    if not parts:
        return None
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]


ROLE_WORDS = {"peak": ("peak window", "peak windows"),
              "control": ("control window", "control windows"),
              "stratum": ("window drawn from a traffic level", "windows drawn from traffic levels"),
              "random": ("random window", "random windows"),
              "chosen by hand": ("window chosen by hand", "windows chosen by hand")}


def _role(v: Mapping[str, Any]) -> str:
    return str((v.get("sampling") or {}).get("role") or "chosen by hand")


def summary(validations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validations of one system together: measures per direction with 95% ranges (by
    validation and by store), broken down by traffic level, by store, and how the windows
    were sampled.

    Each validation: {"id", "store", "camera_hours", "rows": [...], "sampling": its
    sampling.brief() or None (chosen by hand)}.
    """
    rows = [{**r, "validation": v["id"]} for v in validations for r in v["rows"]]
    per_val = [[{**r, "validation": v["id"]} for r in v["rows"]] for v in validations]
    per_store: dict[str, list[dict[str, Any]]] = {}
    for v in validations:
        per_store.setdefault(str(v["store"]), []).extend(
            {**r, "validation": v["id"]} for r in v["rows"])
    main = by_direction(rows)
    for d, m in main.items():
        def stat_for(key: str, d: str = d) -> Callable[[list[Row]], float | None]:
            return lambda sample: metrics(select(sample, d))[key]
        m["ranges"] = {key: {"by_validation": bootstrap(per_val, stat_for(key)),
                             "by_store": bootstrap(list(per_store.values()), stat_for(key))}
                       for key in ("bias_pct", "wape_pct")}
    roles: dict[str, int] = {}
    for v in validations:
        roles[_role(v)] = roles.get(_role(v), 0) + 1
    peak = {str(v["store"]) for v in validations if _role(v) == "peak"}
    control = {str(v["store"]) for v in validations if _role(v) == "control"}
    return {
        "engine": ENGINE,
        "settings": {"min_truth_for_pct": MIN_TRUTH_FOR_PCT,
                     "min_verified_for_pct": MIN_VERIFIED_FOR_PCT, "min_clusters": MIN_CLUSTERS,
                     "bootstrap": BOOTSTRAP_N, "seed": SEED,
                     "traffic_thresholds": [limit for limit, _ in TRAFFIC]},
        "validations": len(validations), "stores": len(per_store),
        "camera_hours": round(sum(float(v.get("camera_hours") or 0) for v in validations), 2),
        "crossings": sum(int(r["truth"]) for r in rows),
        "by_direction": main,
        "by_traffic": by_level(rows),
        "by_store": {s: by_direction(rs) for s, rs in sorted(per_store.items())},
        "by_store_traffic": {s: by_level(rs) for s, rs in sorted(per_store.items())},
        "scope": roles, "stores_without_control": sorted(peak - control),
    }


def scope_text(s: Mapping[str, Any]) -> str:
    """How the windows put together were sampled, and what that lets the result say."""
    roles: Mapping[str, int] = s.get("scope") or {}
    parts = [f"{n} {ROLE_WORDS.get(r, (r, r))[n != 1]}" for r, n in roles.items()]
    if not parts:
        return ""
    text = "Sampled: " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1
                          else parts[0]) + "."
    if set(roles) == {"peak"}:
        text += " Every window was peak trading, so the result describes peak trading."
    if missing := s.get("stores_without_control"):
        text += (f" No control window yet at {', '.join(missing)}: whether the error there "
                 f"comes from crowding cannot be told.")
    return text


def headline(s: Mapping[str, Any], system: str) -> str:
    """The defensible sentence: what was measured, on how much, and how sure."""
    m = s["by_direction"].get(TOTAL) or next(iter(s["by_direction"].values()))
    scope = (f"Across {s['stores']} store{'s' if s['stores'] != 1 else ''}, {s['validations']} "
             f"validation{'s' if s['validations'] != 1 else ''}, {s['camera_hours']} hours of camera "
             f"footage and {s['crossings']} independently verified crossings")
    bias = m["bias_pct"]
    if bias is None:
        return f"{scope}, there were no crossings to compare {system}'s count with."
    sampled = scope_text(s)
    if int(m["truth"]) < MIN_VERIFIED_FOR_PCT:
        q = quote(int(m["truth"]), int(m["system"]))
        return (f"{scope}, {system} counted {m['system']}: {q['words']}. {q['reason']}."
                + (f" {sampled}" if sampled else ""))
    way = "too many" if bias > 0 else "too few" if bias < 0 else "exactly as many"
    rng = m["ranges"]["bias_pct"]["by_validation"]
    sure = (f" (95% range {rng[0]:+.1f}% to {rng[1]:+.1f}%, resampling whole validations)" if rng
            else f" (no uncertainty range: fewer than {MIN_CLUSTERS} validations)")
    return (f"{scope}, {system} counted {abs(bias):.1f}% {way}{sure}. Its typical interval error "
            f"was {m['mae']} people (MAE), and its over- and undercounts together came to "
            f"{m['wape_pct']}% of the verified count (WAPE)." + (f" {sampled}" if sampled else ""))


# ---- results kept by validations -----------------------------------------------------------

def _read(p: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def collect(root: Path, shared: Path | None = None) -> list[dict[str, Any]]:
    """Every validation's saved result (made with its report): the team's shared records,
    then this computer's runs, one per footage (this computer's wins). Results reclassified
    as counted on marked footage (independence.reclassify) come back marked."""
    from .independence import apply, registry

    reg = registry(root)
    return [apply(r, reg) for r in _collect(root, shared)]


def _collect(root: Path, shared: Path | None = None) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if shared is not None:
        for p in sorted((shared / "validations").glob("*/*.json")):
            rec = _read(p) or {}
            if isinstance(rec.get("result"), dict):
                r = rec["result"]
                found[str(r.get("fingerprint"))] = {**r, "where": "shared folder",
                                                    "by": (rec.get("store") or {}).get("operator")}
    for p in sorted((root / "runs").glob("*/wizard/state.json")):
        st = _read(p) or {}
        if isinstance(st.get("result"), dict):
            r = st["result"]
            found[str(r.get("fingerprint"))] = {**r, "where": "this computer",
                                                "by": (st.get("store") or {}).get("operator")}
    return list(found.values())


def exclusions(r: Mapping[str, Any]) -> list[str]:
    """Why a validation's result cannot stand as independent ground truth for a system."""
    why = []
    if r.get("status") != "complete":
        why.append("the validation was incomplete")
    if r.get("marked"):
        again = (r.get("reclassified") or {}).get("why")
        why.append("counted on footage showing the system's own marks"
                   + (f" (found on audit: {'; '.join(again)})" if again else ""))
    if not r.get("rows"):
        why.append("no numbers from the system")
    return why


def overview(root: Path, shared: Path | None = None, system: str | None = None) -> dict[str, Any]:
    results = collect(root, shared)
    systems = sorted({str(r.get("system") or "?") for r in results})
    chosen = system if system in systems else (systems[0] if systems else None)
    mine = [r for r in results if str(r.get("system") or "?") == chosen]
    used: list[dict[str, Any]] = []
    left: list[dict[str, Any]] = []
    for r in mine:
        brief = {"id": str(r.get("fingerprint"))[:12], "store": (r.get("store") or {}).get("code"),
                 "store_name": (r.get("store") or {}).get("name"), "clock_start": r.get("clock_start"),
                 "cameras": r.get("cameras"), "mode": r.get("mode"), "by": r.get("by"),
                 "where": r.get("where"), "made_at": r.get("made_at"),
                 "metrics": r.get("metrics"), "sampling": r.get("sampling"),
                 "traffic": r.get("traffic")}
        why = exclusions(r)
        (left if why else used).append({**brief, "excluded": why})
    vals = [{"id": str(r.get("fingerprint")), "store": (r.get("store") or {}).get("code"),
             "camera_hours": float(r.get("duration_s") or 0) * len(r.get("cameras") or [1]) / 3600,
             "rows": r["rows"], "sampling": r.get("sampling")} for r in mine if not exclusions(r)]
    s = summary(vals) if vals else None
    return {"engine": ENGINE, "systems": systems, "system": chosen, "summary": s,
            "headline": headline(s, str(chosen)) if s else None,
            "levels": level_sentence(s["by_traffic"], str(chosen)) if s else None,
            "validations": used, "excluded": left, "shared": str(shared) if shared else None}
