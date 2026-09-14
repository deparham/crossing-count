# Ground Truth Specification v1.0

Status: in force from 14 September 2026. Every count by hand records the version it
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
direction, at most 2 seconds apart). Crossings both counted are the ground truth;
the moments they disagree on stay out of scoring until adjudicated, and both original
counts are kept.

## 7. The automatic counter's rule is not this definition

The automatic counter (rule.py) uses heuristics of its own: a mask zone a person must
reach, filter zones, and cancelling a crossing that the same track undoes. Those decide
what the tool *proposes*. They do not define a crossing: this document does.

## Change log

| Version | Date | Change |
|---|---|---|
| 1.0 | 14 Sep 2026 | First version. |
