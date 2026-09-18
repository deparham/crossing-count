"""Scoring the automatic count against crossings a person counted in full.

A crossing is a moment on one camera, with a direction. A tool crossing and a person's
crossing are the same event when they are on the same camera and at most TOLERANCE_S
apart. Each is matched at most once, so one real crossing can never make two correct
counts, and the most pairs possible are made (people crossing close together included).
Same-direction pairs are made first; only what is left is paired across directions.

    found       matched, same direction
    wrong way   matched, but the tool gave the other direction
    missed      a person's crossing the tool did not count at all
    false       a tool crossing with no person's crossing near it
    duplicate   a false one near a crossing already found (a part of false)
    ignored     a tool crossing near a crossing marked uncertain: neither right nor wrong

    recall      found / the person's crossings (per direction: of that direction)
    miss rate   missed / the person's crossings
    precision   found / the tool's crossings (ignored ones left out)
    F1          2 x precision x recall / (precision + recall)

Recall, wrong way and miss rate add up to 100%. None of these is "accuracy": sensor
accuracy compares RetailNext's count with the verified count, a different question.
Every rate comes with a 95% interval, and none is given on fewer than MIN_SAMPLE
crossings: "insufficient sample" instead.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from typing import Any

ENGINE = "crossing-evaluation/1.0"
MATCHING = "one-to-one time sweep, same direction first, clocks aligned/1.1"
# A person counting by hand presses the key after seeing the crossing: measured on the first
# hand counts (30 crossings, two stores, three cameras, 18 Sep 2026) the press came a median
# 1.4 s after the tool's moment, the middle half between 0.1 s and 3.1 s. A 2 s window caught
# barely half of those pairs, so the same person's crossing was scored as a miss and a false
# count at once. Crossings on one camera are a median 5.5 s apart, so a wider window costs
# little: the ambiguity that remains is people crossing together, which no window settles.
TOLERANCE_S = 3.0
LAG_WINDOW_S = 8.0  # pairs this close count towards the offset between the two clocks
LAG_MIN_PAIRS = 8  # fewer than this and the offset says more about chance than about the clocks
LAG_MAX_S = 3.0  # a larger offset is not a clock to align but a disagreement to look at
MIN_SAMPLE = 30
DIRS = ("in", "out")
KEYS = ("truth", "pred", "found", "wrong_way", "missed", "false", "duplicate", "as_other",
        "ignored")


def match(truth: Sequence[float], pred: Sequence[float], tol: float = TOLERANCE_S
          ) -> list[tuple[int, int]]:
    """The most one-to-one pairs (truth index, pred index) at most tol apart.

    Every window being the same width, giving each person's crossing, in time order, the
    earliest tool crossing still free in its window makes the most pairs.
    """
    ti = sorted(range(len(truth)), key=lambda i: truth[i])
    pi = sorted(range(len(pred)), key=lambda j: pred[j])
    out: list[tuple[int, int]] = []
    k = 0
    for i in ti:
        while k < len(pi) and pred[pi[k]] < truth[i] - tol:
            k += 1  # too early for this crossing, so for every later one too
        if k < len(pi) and pred[pi[k]] <= truth[i] + tol:
            out.append((i, pi[k]))
            k += 1
    return out


def lag(truth: Iterable[tuple[float, str]], pred: Iterable[tuple[float, str]],
        dirs: Sequence[str] = DIRS, window: float = LAG_WINDOW_S,
        min_pairs: int = LAG_MIN_PAIRS, max_shift: float = LAG_MAX_S) -> float:
    """How far the tool's clock sits from the person's, in seconds, to add to the tool's times
    before matching (positive: the tool counted earlier than the person pressed).

    A count made by hand carries the counter's reaction time, which is theirs, not the tool's
    error. The offset is the median of the gaps between each crossing and the tool's nearest,
    counted only where they are close enough to be the same person. It is 0 on too few pairs,
    or when the gap is too large to be a reaction: then there is something else to look at,
    and the comparison should show it rather than hide it."""
    real = sorted(t for t, d in truth if d in dirs)
    tool = sorted(t for t, d in pred if d in dirs)
    if not real or not tool:
        return 0.0
    gaps = [g for t in real if abs(g := min(tool, key=lambda x: abs(x - t)) - t) <= window]
    if len(gaps) < min_pairs:
        return 0.0
    shift = -statistics.median(gaps)
    return round(shift, 2) if abs(shift) <= max_shift else 0.0


def score(truth: Iterable[tuple[float, str]], pred: Iterable[tuple[float, str]],
          dirs: Sequence[str] = DIRS, uncertain: Iterable[float] = (),
          tol: float = TOLERANCE_S) -> dict[str, Any]:
    """One camera: each crossing counted as found, wrong way, missed, false or ignored.

    Returns {"by_direction": {dir: counts}, "at": {kind: [seconds]}}.
    """
    real = [(float(t), d) for t, d in truth if d in dirs]
    tool = [(float(t), d) for t, d in pred if d in dirs]
    unsure = sorted(float(t) for t in uncertain)
    counts = {d: dict.fromkeys(KEYS, 0) for d in dirs}
    at: dict[str, list[float]] = {k: [] for k in ("missed", "false", "duplicate", "wrong_way",
                                                  "ignored")}
    found_t: dict[int, int] = {}  # truth index -> pred index
    used_p: set[int] = set()
    for d in dirs:
        ti = [i for i, (_, x) in enumerate(real) if x == d]
        pj = [j for j, (_, x) in enumerate(tool) if x == d]
        for a, b in match([real[i][0] for i in ti], [tool[j][0] for j in pj], tol):
            found_t[ti[a]] = pj[b]
            used_p.add(pj[b])
            counts[d]["found"] += 1
    left_t = [i for i in range(len(real)) if i not in found_t]
    left_p = [j for j in range(len(tool)) if j not in used_p]
    wrong_t: set[int] = set()
    for a, b in match([real[i][0] for i in left_t], [tool[j][0] for j in left_p], tol):
        i, j = left_t[a], left_p[b]
        # a same-direction pair cannot be left over: the matching above made the most pairs
        counts[real[i][1]]["wrong_way"] += 1
        counts[tool[j][1]]["as_other"] += 1
        at["wrong_way"].append(real[i][0])
        wrong_t.add(i)
        used_p.add(j)
    for i in left_t:
        if i not in wrong_t:
            counts[real[i][1]]["missed"] += 1
            at["missed"].append(real[i][0])
    for j in range(len(tool)):
        if j in used_p:
            continue
        t, d = tool[j]
        if any(abs(t - u) <= tol for u in unsure):
            counts[d]["ignored"] += 1
            at["ignored"].append(t)
            continue
        counts[d]["false"] += 1
        at["false"].append(t)
        if any(real[i][1] == d and abs(t - real[i][0]) <= tol for i in found_t):
            counts[d]["duplicate"] += 1
            at["duplicate"].append(t)
    for d in dirs:
        counts[d]["truth"] = sum(1 for _, x in real if x == d)
        counts[d]["pred"] = sum(1 for _, x in tool if x == d) - counts[d]["ignored"]
    return {"by_direction": counts, "at": {k: sorted(round(v, 2) for v in vs)
                                           for k, vs in at.items()}}


def add(total: dict[str, dict[str, int]], more: dict[str, dict[str, int]]) -> None:
    """Add one camera's (or clip's) counts to a running total, direction by direction."""
    for d, c in more.items():
        acc = total.setdefault(d, dict.fromkeys(KEYS, 0))
        for k in KEYS:
            acc[k] += int(c.get(k, 0))


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """The 95% interval of a proportion k/n, in percent (Wilson's: sound for small n)."""
    if not n:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1)]


def _pct(k: int, n: int) -> float | None:
    return round(100.0 * k / n, 1) if n else None


def rates(c: dict[str, int], min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """Recall, miss rate, precision and F1 of one set of counts, or why they are not given."""
    n, p = c["truth"], c["pred"]
    out: dict[str, Any] = {"sufficient": n >= min_sample, "sample": n}
    if n < min_sample:
        out["reason"] = f"Insufficient sample: {n} crossing(s), at least {min_sample} needed."
    recall, precision = _pct(c["found"], n), _pct(c["found"], p)
    f1 = (round(2 * precision * recall / (precision + recall), 1)
          if recall is not None and precision is not None and precision + recall > 0 else None)
    out.update(recall=recall, recall_ci=wilson(c["found"], n),
               miss_rate=_pct(c["missed"], n), wrong_way_rate=_pct(c["wrong_way"], n),
               precision=precision, precision_ci=wilson(c["found"], p), f1=f1,
               duplicate_rate=_pct(c["duplicate"], p))
    return out


def summary(by_dir: dict[str, dict[str, int]], min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """Counts and rates per direction and for both together."""
    both = dict.fromkeys(KEYS, 0)
    for c in by_dir.values():
        for k in KEYS:
            both[k] += c[k]
    return {"by_direction": {d: {**c, **rates(c, min_sample)} for d, c in by_dir.items()},
            "all": {**both, **rates(both, min_sample)}}


def consensus(a: Iterable[tuple[float, str]], b: Iterable[tuple[float, str]],
              dirs: Sequence[str] = DIRS, tol: float = TOLERANCE_S
              ) -> tuple[list[tuple[float, str]], list[float]]:
    """Two people's counts of the same footage as one: the crossings both counted the same
    way (at the middle of their two times), and the moments they disagree on. Those are
    not settled here: they are left out of any scoring as uncertain."""
    xs = [(float(t), d) for t, d in a if d in dirs]
    ys = [(float(t), d) for t, d in b if d in dirs]
    agreed: list[tuple[float, str]] = []
    used_x: set[int] = set()
    used_y: set[int] = set()
    for d in dirs:
        ix = [i for i, (_, x) in enumerate(xs) if x == d]
        iy = [j for j, (_, y) in enumerate(ys) if y == d]
        for p, q in match([xs[i][0] for i in ix], [ys[j][0] for j in iy], tol):
            agreed.append((round((xs[ix[p]][0] + ys[iy[q]][0]) / 2, 2), d))
            used_x.add(ix[p])
            used_y.add(iy[q])
    disputed = sorted([t for i, (t, _) in enumerate(xs) if i not in used_x]
                      + [t for j, (t, _) in enumerate(ys) if j not in used_y])
    return sorted(agreed), disputed


def agreement(a: Iterable[tuple[float, str]], b: Iterable[tuple[float, str]],
              dirs: Sequence[str] = DIRS, tol: float = TOLERANCE_S) -> dict[str, Any]:
    """Two people's counts of the same footage, matched the same way as the tool's."""
    s = score(a, b, dirs, (), tol)
    total = dict.fromkeys(KEYS, 0)
    for c in s["by_direction"].values():
        for k in KEYS:
            total[k] += c[k]
    agreed, wrong = total["found"], total["wrong_way"]
    only_a, only_b = total["missed"], total["false"]
    union = agreed + wrong + only_a + only_b
    return {"agreed": agreed, "direction_disagreements": wrong, "only_first": only_a,
            "only_second": only_b, "agreement_pct": _pct(agreed, union),
            "disagreements": union - agreed,
            "at": {"direction": s["at"]["wrong_way"], "only_first": s["at"]["missed"],
                   "only_second": s["at"]["false"]}}
