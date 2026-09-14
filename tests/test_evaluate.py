"""Crossing-level scoring: every crossing sorted once, rates named for what they measure."""

from __future__ import annotations

from crossing_count.evaluate import agreement, match, rates, score, summary, wilson


def test_matching_is_one_to_one_and_makes_the_most_pairs() -> None:
    assert sorted(match([10.0, 11.5], [10.9, 12.8], tol=2.0)) == [(0, 0), (1, 1)]
    assert len(match([10.0, 11.0], [10.5], 2.0)) == 1  # one tool crossing, two people
    assert match([10.0], [12.5], 2.0) == []
    # pairing the closest first (11.0 with 11.2) would leave 9.5 and 13.1 alone: two pairs exist
    assert len(match([9.5, 11.2], [11.0, 13.1], 2.0)) == 2


def test_every_crossing_is_sorted_once() -> None:
    truth = [(10, "in"), (20, "in"), (30, "out"), (40, "out")]
    pred = [(10.5, "in"), (11.0, "in"), (20.3, "out"), (55, "out"), (60, "in")]
    s = score(truth, pred, uncertain=[60.5])
    i, o = s["by_direction"]["in"], s["by_direction"]["out"]
    assert (i["truth"], i["found"], i["wrong_way"], i["missed"]) == (2, 1, 1, 0)
    assert (i["pred"], i["false"], i["duplicate"], i["ignored"]) == (2, 1, 1, 1)
    assert (o["truth"], o["found"], o["missed"], o["pred"], o["as_other"], o["false"]) == (2, 0, 2, 2, 1, 1)
    assert s["at"] == {"missed": [30.0, 40.0], "false": [11.0, 55.0], "duplicate": [11.0],
                       "wrong_way": [20.0], "ignored": [60.0]}
    for c in (i, o):  # nothing counted twice, nothing lost
        assert c["truth"] == c["found"] + c["wrong_way"] + c["missed"]
        assert c["pred"] == c["found"] + c["as_other"] + c["false"]


def test_rates_say_what_they_measure_and_refuse_small_samples() -> None:
    c = {"truth": 40, "pred": 38, "found": 36, "wrong_way": 1, "missed": 3, "false": 1,
         "duplicate": 1, "as_other": 1, "ignored": 0}
    r = rates(c)
    assert (r["recall"], r["miss_rate"], r["wrong_way_rate"]) == (90.0, 7.5, 2.5)
    assert r["precision"] == 94.7 and r["f1"] == 92.3 and r["sufficient"]
    lo, hi = r["recall_ci"]
    assert lo < 90 < hi
    small = rates({**c, "truth": 10, "found": 9, "missed": 1, "wrong_way": 0})
    assert not small["sufficient"] and small["reason"].startswith("Insufficient sample")
    assert wilson(0, 0) is None
    both = summary({"in": c, "out": c})["all"]
    assert both["truth"] == 80 and both["recall"] == 90.0


def test_two_reviewers_are_compared_the_same_way() -> None:
    g = agreement([(10, "in"), (20, "out"), (30, "in")], [(10.4, "in"), (20.2, "in"), (45, "out")])
    assert (g["agreed"], g["direction_disagreements"], g["only_first"], g["only_second"]) == (1, 1, 1, 1)
    assert g["agreement_pct"] == 25.0 and g["at"]["only_first"] == [30.0]
    assert g["at"]["direction"] == [20.0] and g["at"]["only_second"] == [45.0]
