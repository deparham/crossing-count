# Ground Truth Specification v1.1

Status: v1.1 in force from 15 September 2026 (v1.0 from 14 September 2026). Every count by
hand records the version it
followed (`specification` in the wizard's state and in each gold clip). Counts made
under different versions are not scored together unless the change log below says the
versions are compatible.

This document says what a crossing is, for the people who count footage by hand. It
is the definition the automatic counter and the sensor under test are measured
against. It is deliberately independent of both: nothing here depends on what the
tool proposed or what the sensor counted.

## 1. Terms

| Term | Meaning |
|---|---|
| Camera | One camera's picture. A video can hold several (tiles); each is counted on its own. |
| Counting line | The line drawn for a camera in CrossingCount (or, on a sensor's own footage, the sensor's line). A polyline on the floor, running across the entrance. |
| Side A | The outside: the mall, street or corridor. |
| Side B | The inside of the store: the side the line's triangles point to. |
| Person | A human being moving on their own feet or in a wheelchair or mobility scooter. See section 4 for children, staff and people carried. |
| Floor point | The point on the floor under the middle of the person's body (between the feet). On an overhead camera near the centre of the picture it is under the head; towards the edge of a wide-angle picture it is where the feet are, not where the head appears. |
| Clip | The stretch of footage being counted, from its first to its last frame. |

## 2. What a crossing is

A **crossing** happens when a person's floor point moves from one side of the counting
line to the other and the crossing is **completed**: after passing the line, the person
either goes on at least one full step (about half a metre) clear of the line on the new
side, or leaves the picture on the new side.

- **Direction.** Side A to side B is **IN**. Side B to side A is **OUT**.
- **Time.** A crossing's time is the moment the floor point passes over the line (the
  last time it does so, if the person hesitated on it). Press the key as the person
  crosses; slow the video down or pause when several people cross together.
- **Only the line counts.** A person who walks round the end of the line, or along it,
  without their floor point passing over it, has not crossed.
- **Where the picture ends.** If the line runs to the edge of the picture, a person who
  crosses where the picture cuts off counts only if they are seen on both sides of the
  line.

## 3. Rules for difficult cases

| Case | Rule |
|---|---|
| Stops on the line | Nothing yet. If they then complete a crossing, it is one crossing, timed when they finally pass the line. If they go back, there is no crossing. |
| Turns back before completing | No crossing (they did not get a step clear of the line). |
| Crosses, completes, and later comes back | Two crossings: one each way. |
| Group crossing together | Each person is one crossing. Watch at normal speed or frame by frame until the number is certain. |
| Crossings at the same moment, either direction | Each person is one crossing. |
| Partly out of the picture | Count if their floor point can be placed on each side of the line before and after; otherwise mark **uncertain**. |
| Hidden while crossing (occlusion) | Count if the same person is seen on one side, then on the other, within about 3 seconds and there is no doubt it is the same person; otherwise mark **uncertain**. |
| Enters or leaves the picture without crossing the line | No crossing. |
| Running, or very fast | Counted like anyone else; slow the video down. |
| Wheelchair, mobility scooter, crutches | One person, counted. |
| Pram, stroller, trolley | The person pushing it counts. The pram, and a child in it, do not. |
| Carried child (in arms, on shoulders, in a carrier) | Not counted. The carrier counts. |
| Mannequin, poster, screen, reflection (glass, mirror, shiny floor), shadow | Never counted. |
| Animal, object, trolley on its own | Never counted. |
| Cannot decide | Mark **uncertain** with a note. Never guess. Uncertain crossings are not counted, and they are left out of any scoring (neither a hit nor a miss). |

Tracks, boxes and counts drawn by the tool or by a sensor are never evidence. The
count comes from the people in the picture only.

## 4. What counts as a person: set for each validation

A sensor is often set up not to count some people. The ground truth must follow the same
definition, or the sensor is marked wrong for doing what it was told. Each count by hand
records these settings, chosen on the wizard's traffic step:

| Setting | Counted (default) | Not counted |
|---|---|---|
| Children | Every child walking on their own | Children walking on their own who are clearly below about 1.2 m (roughly an adult's chest height), when the sensor is set to leave them out |
| Staff | Everyone | People clearly working in the store (uniform, name badge, or going behind the counter), when the sensor is set to leave them out |

If a person's category is unclear (a tall child, someone who may be staff), count them
and add a note; if the setting says not to count them and it matters, mark uncertain.

## 5. How the counting is done

1. **Clean footage.** Count footage without the sensor's own drawings. The sensor's
   tracks, boxes and counts on the picture are the system under test and can sway the
   count towards it. (CrossingCount downloads clean footage for a count by hand and
   draws its own line. Footage with the sensor's marks can still be looked at, but a
   count made on it is never a gold clip.)
2. **Nothing else shown.** While counting, the annotator sees neither the automatic
   counter's crossings nor the sensor's numbers. The sensor's numbers are entered only
   after the count is finished.
3. **Every moment.** Each camera is watched from start to end: at least 99% of its
   footage, which the counter measures. Normal traffic at up to 2x; groups and busy
   moments at 1x or slower. Replay anything unsure.
4. **One camera at a time.** Each camera's picture is counted on its own, even when
   cameras overlap. Whether overlapping cameras double-count is a question for the
   comparison, not for the count.
5. **Notes.** Anything unusual (a group whose size is unsure, a staff member, a
   reflection that looks like a person) gets a note.

## 6. Two annotators

A gold clip can be counted a second time, independently, by someone who has not seen the
first count. The two counts are matched crossing by crossing (same camera, same
direction, at most 2 seconds apart). Crossings both counted are the ground truth. Each
moment they disagree on (a crossing only one counted, one counted in opposite
directions, or one marked uncertain) is listed on the Count by hand step, with a Show
button. A third person, ideally, settles each one after watching it: **In**, **Out**,
**no crossing**, or **still uncertain**, always with the reason. Until settled, a moment
stays out of scoring. Both original counts are never changed; every decision is kept with
who made it, when and why, in the clip's history and in the audit log.

## 7. Marking and correcting, in the app

While counting by hand: `I` / `O` count a crossing; with `Shift` (`⇧I` / `⇧O`) the crossing
is marked **uncertain**: kept and listed in the report, never counted, and left out of
scoring. `U` marks (or unmarks) the last crossing uncertain; `E` opens it to correct its
direction, move it to the moment the video is paused on, mark it uncertain or add a note;
`,` and `.` step one frame. Every correction is kept with what it was, what it became and
the reason given.

## 8. The automatic counter's rule is not this definition

The automatic counter (rule.py) uses heuristics of its own: a mask zone a person must
reach, filter zones, and cancelling a crossing that the same track undoes. Those decide
what the tool *proposes*. They do not define a crossing: this document does.

## 9. Sampling: which footage is counted

A validation measures the counting system during the footage that was counted, and nothing
more. How that footage was picked decides what the result may be said to describe, so it is
picked by a stated rule (`sampling.py`), and the rule is recorded with the validation.

| Mode | Windows | What the result describes |
|---|---|---|
| **peak** (the default) | the busiest windows of the day for the traffic validated, and at least one **control window** of lower traffic, drawn at random | peak trading; with the control, whether the error comes from crowding |
| **stratified** | one window drawn at random from each traffic level the day has | each traffic level |
| **random** | windows drawn at random from the trading hours | the trading day, once enough are counted |

None is a better sample than another: they answer different questions.

- **Peak is the default.** Peak trading is what clients ask about, where occupancy
  decisions are made, and where sensors fail; quiet periods measure the easy case. A peak
  result is a valid accuracy measurement **of peak trading**, and is always worded that way:
  "At peak trading (the busiest 15 minutes, busy traffic), RetailNext undercounts by 6%",
  never "RetailNext is 94% accurate at this store".
- **Control windows.** Every engagement that validates peak windows also validates at least
  one window from a lower traffic level (normal traffic first, then quiet), drawn at random
  clear of the peak windows. The control does not dilute the headline: it turns "the sensor
  is 6% out" into "the sensor is accurate at normal traffic and undercounts 6% at peak",
  which points at crowding as the cause. The Sensor results page names every store with peak
  windows and no control window yet.
- **Traffic levels.** Quiet under 40 crossings per camera-hour, normal under 120, busy under
  240, heavy from 240 (the directions validated, together). When windows are chosen, the only
  numbers there are the system's own, so a window's level at selection comes from them. The
  result's level comes from the verified count, interval by interval, and the error is
  reported per level (docs/METRICS_SPECIFICATION.md, section 5).
- **Windows.** Whole consecutive 15-minute intervals of the system's own data, within trading
  hours, so the comparison is exact. Intervals the system marked incomplete or imputed are
  drawn last.
- **Recorded.** Every validation's manifest records the mode, the seed of the random draws,
  every candidate window considered, the windows chosen with the traffic level of each, and
  which window this footage is. Footage picked any other way is recorded as **chosen by
  hand**, and its report says it describes that period only.
- **Said.** Every report states its sampling scope on its first page, in plain words, next
  to the headline number, with the error at each traffic level.
- **Independence.** While choosing, windows are shown by their role and traffic level only,
  never with the system's numbers.

## Change log

| Version | Date | Change |
|---|---|---|
| 1.0 | 14 Sep 2026 | First version. |
| 1.1 | 15 Sep 2026 | Section 9: sampling protocol (peak with control windows, stratified, random), recorded with every validation and stated on every report. What a crossing is did not change: counts made under 1.0 and 1.1 are scored together. |
