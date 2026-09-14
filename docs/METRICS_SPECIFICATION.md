# Metrics Specification

Every number CrossingCount reports, what it means, how it is computed, and where it is
tested. Two different questions are measured, and they are never mixed:

- **The system under test** (RetailNext, or any counter): how far its **counts** are from
  the verified counts. Most counters give a count per interval, not individual crossings,
  so their precision and recall cannot be computed; their count error can
  (section 2). Engine: `validation.py`, version `count-validation/1.0`.
- **CrossingCount's own automatic counter**: which real crossings it finds, crossing by
  crossing, against gold clips (section 1). Engine: `evaluate.py`, version
  `crossing-evaluation/1.0`.

No metric is called "accuracy" without a definition: the report's **sensor accuracy**
card is defined in section 3.

## 1. Crossing level (the automatic counter)

**Matching** (`evaluate.match`, `evaluate.score`; matching version
"one-to-one time sweep, same direction first/1.0"). A tool crossing and a person's
crossing are the same event when they are on the same camera and at most `TOLERANCE_S`
(2 s) apart. Each is matched at most once. Same-direction pairs are made first, taking
each person's crossing in time order and giving it the earliest free tool crossing in its
window (with equal windows this makes the most pairs possible, including people crossing
close together); only what is left is paired across directions. The result is the same
whatever order the crossings are given in.

| Term | Definition |
|---|---|
| found (TP) | matched, same direction |
| wrong way | matched, other direction |
| missed (FN) | a person's crossing not matched at all |
| false (FP) | a tool crossing not matched and not near an uncertain moment |
| duplicate | a false one within the tolerance of a crossing already found |
| ignored | a tool crossing near a moment marked uncertain: neither right nor wrong |

| Metric | Formula | Edge cases |
|---|---|---|
| Recall | found / person's crossings | none when there are no crossings |
| Miss rate | missed / person's crossings | recall + wrong-way rate + miss rate = 100% |
| Wrong-way rate | wrong way / person's crossings | |
| Precision | found / tool crossings (ignored left out) | none when the tool counted nothing |
| F1 | 2 PR / (P + R) | none unless both exist |
| Duplicate rate | duplicate / tool crossings | |

Per direction (In, Out) and for both together. **No rate is given on fewer than 30
crossings**: "insufficient sample" instead. Tests: `tests/test_evaluate.py`.

**Two people's counts** (`evaluate.agreement`, `evaluate.consensus`) are matched the same
way: agreed, direction disagreements, counted only by the first, only by the second.
Agreement % = agreed / (agreed + disagreements). Kappa is not used: crossing events have no
true negatives to count. Crossings both counted become the truth (at the middle of their
two times); disagreements are left out as uncertain and listed.

## 2. Count level (the system under test)

Rows are normalised interval counts (`sensors.py` translates each source):
`{interval, direction, truth, system}`, truth being the verified count. Only whole
intervals take part: an interval the footage covers only partly is left out, since the
system's number covers all of it.

| Metric | Formula | Meaning | Edge cases |
|---|---|---|---|
| Error e (per interval) | system - truth | positive: the system counted too many | |
| Total error E | sum e | the net difference | over- and undercounts cancel |
| Bias % | 100 E / sum truth | the net difference as a share of the truth | none if truth is 0 |
| Overcount | sum of positive e | people counted who were not there (net per interval) | |
| Undercount | sum of -e where e < 0 | people missed (net per interval) | |
| MAE | mean \|e\| | typical error in one interval, in people | none with no intervals |
| RMSE | sqrt(mean e^2) | like MAE, weighting large errors more | |
| WAPE % | 100 sum \|e\| / sum truth | error that does not cancel, as a share of the truth | none if truth is 0 |
| MAPE % | mean of 100 \|e\| / truth over intervals with truth >= 10 | percentage error of a typical busy interval | intervals under 10 crossings are left out (1 wrong of 2 would be 50%); the number used is shown |
| Max \|e\| | largest interval error | | |

Per direction (In, Out) and **total** (each interval's In and Out summed before the error is
taken). Tests: `tests/test_validation.py`.

## 3. Sensor accuracy (the report's card)

Per direction, for one validation: 100% - |system - verified| / verified x 100, floored at
0. It is a symmetric agreement score: 90% means the system was 10% away from the verified
count, over or under. Undefined when nobody crossed (shown as 100% only if the system also
said 0). With crossings answered "unsure", the card gives the range over every way they
could go. When the validation is incomplete, no figure is given ("INCOMPLETE"). It is not
the automatic counter's accuracy and not a precision or recall.

## 4. Uncertainty

- **Crossing-level rates**: Wilson score intervals (95%, z = 1.96), sound for small samples
  and near 0 or 100%.
- **Count errors across validations**: a percentile bootstrap (2,000 resamples, fixed seed
  20260914, 2.5th to 97.5th percentile) that resamples **whole validations**, and
  separately **whole stores**, with replacement. Intervals of one clip share a camera, a day
  and a crowd, so resampling them one by one would claim more certainty than there is.
  With fewer than 5 validations (or stores), no range is given. Given for bias % and WAPE %.
- A single validation gets no uncertainty range: one clip cannot show how much the result
  would move on another day.

## 5. Traffic levels

Crossings per camera-hour: quiet below 40, normal below 120, busy below 240, heavy from
240. Provisional thresholds, stored with every result that uses them.

## 6. What every result records

Crossing-level scorings (gold page, `bench/experiments/`): engine and matching versions,
tolerance, minimum sample, Ground Truth Specification version, dataset version and content
hash with each clip's checksum, app version, detector and its weights' SHA-256, tracking
and rule settings. Count-level summaries: engine version, the minimum truth for
percentages, minimum clusters, bootstrap size and seed, traffic thresholds; each
validation's result records its app version, engine version, status, what counted as a
person, the specification version, the system and where its numbers came from.
