# Dataset Specification: the gold set (gold_v1)

The gold set holds clips a person counted in full, by hand, under the
[Ground Truth Specification](GROUND_TRUTH_SPECIFICATION.md). It is the independent
ground truth the automatic counter is scored against (and, when a clip also has the
sensor's numbers, the sensor too). Models never write to it.

## 1. On disk

Everything is in the data folder (the project folder, or
`~/Library/Application Support/CrossingCount` / `%LOCALAPPDATA%\CrossingCount` for the
installed app). It is never in git: it is counts of a retailer's footage.

```
gold/gold_v1/
  <clip-id>.json          one clip (schema gold/1)
  <clip-id>-marked.json   the same window, counted on footage showing RetailNext's marks
  manifests/
    gold_v1.1.json        frozen versions: never rewritten
    gold_v1.2.json
```

**Shared with the team.** With a shared folder set on the report page (a SharePoint
library synced by OneDrive, or a network share), every clip kept is also copied to
`<shared>/gold/gold_v1/`, and clips found there are part of the set on every computer
(this computer's own copy wins when both have a clip). The same folder receives each
validation's record, `<shared>/validations/<store>/<video>.json` (answers, counts by
hand, the sensor's numbers, the decision log; no video), and the learning examples
(`<shared>/<store>/<video>/...`, `index.jsonl`), each naming its store's set and
`train_ok: false` for test-set stores. They hold pictures of people: the folder must be
one only the team can open.

A clip's id is `<store code>-<YYYYMMDD-HHMMSS>-<minutes>m` (its start on the footage's
clock and its length), so the same store and period always make the same clip: a second
download of the same period, counted by a second person, becomes that clip's second
count, not a new clip. A count on marked footage of the same window is a clip of its own,
`<id>-marked`: clean and marked counts are never mixed in one clip.

## 2. A clip

| Field | Meaning |
|---|---|
| `schema`, `dataset`, `id` | `gold/1`, `gold_v1`, the clip id |
| `store` | `{code, name}` |
| `split` | `train`, `validation` or `test`: the store's set (section 4) |
| `period` | `{start, end, tz}` on the footage's clock (from RetailNext's file name) |
| `duration_s` | length of the clip |
| `video` | `{filename, fingerprint, fps, width, height}` of the footage counted first |
| `cameras` | `[{sensor, picture, config}]`: each camera counted, its picture in the video and its drawing |
| `dirs` | the traffic counted: `["in"]`, `["out"]` or both |
| `direction_convention` | In = into the store: the side the counting line's triangles point to |
| `specification` | the Ground Truth Specification version the counts followed (`1.0`) |
| `tier` | `clean` or `marked`: what the footage was checked to be (section 2) |
| `rules` | `{children, staff}`: `count` or `exclude`, as chosen for the validation (spec section 4) |
| `conditions` | `{traffic, traffic_per_camera_hour, lighting, occlusion}` (section 3) |
| `tags` | scenario tags chosen by the person who counted (groups, people stopping, ...) |
| `notes` | free text |
| `reviews` | one or two independent counts: `{reviewer, at, run, video, marked, footage, watched_pct, crossings: [{camera, t, clock, direction}], system_counts}`; `footage` is what the count's footage was checked to be (clean or not, how it was obtained, what each picture showed); `system_counts` is `{system, counts: {in, out}}`, the system's own number for exactly this window (whole 15-minute intervals only), or null |
| `created_at`, `updated_at`, `made_with` | when, and which CrossingCount version |

Only counts that meet all of these are accepted: counted by hand in the wizard's Manual
mode; at least 99% of every counted camera's footage watched; the footage checked in the
picture itself for RetailNext's marks (Ground Truth Specification, section 10.1); the
footage's clock known; store code and the counter's name given. A second count must cover
the same cameras and traffic, under the same rules, on the same kind of footage.

**Clean and marked clips.** Each clip is one of two tiers, from what its footage was checked
to be (the picture first, then how it was obtained, its name, and what the counter said):

| Tier | Footage | Scored |
|---|---|---|
| `clean` | nothing of the sensor's on the picture | development and test sets |
| `marked` | RetailNext's tracks and counts on the picture, which can sway a count towards the sensor's | development set only, always also shown on its own row; never in the test set, and never in anything said about the sensor's accuracy |

A development score over both tiers counts a window counted both ways once, from its
clean count, and says how many clips of each it rests on. "Clean footage only" scores
without the marked clips. A clip kept before tiers is marked if any of its counts was on
marked footage.

**Provisional clips.** A clip said to be clean whose footage was never checked for marks in
the picture (kept before that check existed) is provisional: it stays in the set and is
listed everywhere, with the reason, but is left out of scoring unless it is asked for.
Saving the count again checks its footage. Frozen manifests list provisional clips, and
every scoring says how many it left out.

## 3. Conditions

| Field | Values | How it is set |
|---|---|---|
| `traffic` | `quiet`, `normal`, `busy`, `heavy` | Worked out from the count: crossings per camera-hour below 40, below 120, below 240, 240 or more. The thresholds are provisional and are stored with each clip. |
| `lighting` | `normal`, `low`, `glare`, `mixed` | Chosen by the person who counted |
| `occlusion` | `none`, `some`, `heavy` | Chosen by the person who counted |

Results are broken down by these and by tags, and a group with fewer than 30 crossings
gets no rate ("insufficient sample").

## 4. Train, validation and test

Sets are assigned **by store**, never by frame or by clip: all of a store's clips are in
one set. A store's set is fixed by its code: the SHA-256 of the code (trimmed, upper
case) modulo 100 puts it in test below 20, validation below 40, and train otherwise. The
rule needs no shared list, so every computer in a team agrees on it, and it never
changes. Learning examples and validation records carry the same set, so a model is
never trained on a test-set store.

- **Development** (train and validation) is for measuring changes and tuning.
- **Test** is held back for a final check of a settled version. It is scored only on
  request, on clean clips only, and every scoring of it is recorded. Tuning on it makes its
  result worthless. A marked clip of a test-set store is kept but never scored.

With few stores the sets are small and uneven; every result says how many stores it
covers.

## 5. Versions

The working set grows as clips are kept. **Freezing** it writes a manifest to
`manifests/gold_v1.<n>.json`, never rewritten afterwards:

```
dataset_id: gold_v1
dataset_version: gold_v1.3
created_at, note
ground_truth_versions: ["1.0"]
content_hash: sha256 over the clip files listed
stores, cameras, videos (fingerprints), crossings
splits: {store: set}
tiers: {clean: n, marked: n}
clips: [{id, split, tier, sha256, crossings}]
```

Every scoring records the manifest of what it scored: the frozen version it matches
exactly, or `unreleased` with its content hash. A frozen version can always be checked:
if a listed clip's file no longer has the listed hash, the check says so.

## 6. Checks

The gold page runs these every time it opens:

- every clip file reads and has schema `gold/1`;
- every clip's set is its store's set, and no store is in two sets;
- every frozen manifest still matches the clip files it lists.

## 7. How far RetailNext's marks sway a count

A window with both a clean and a marked clip is a **pair**. For each pair the gold page shows:
- **Crossings matched:** the two counts are matched crossing by crossing, as two people's
  counts are (at most 3 s apart on the same camera, after the clocks are aligned). Moments either marked uncertain are
  left out.
- **Only one count:** the crossings only the clean count found, and those only the marked
  count found.
- **Each direction's count:** the count on both footages. Where the system's own number
  for the window is known, it also shows whether the marked count moved towards that
  number, away from it, or no nearer.
- **Who counted:** whether the same person counted both, and how many days apart. Within
  a week, remembering the first count can make the two agree more than the marks alone
  would, so such pairs are flagged.

Nothing is concluded from fewer than 5 comparable pairs; from 5, the page states what the
pairs show, and the reader judges. Pairs counted by different people, or weeks apart, say
the most.
