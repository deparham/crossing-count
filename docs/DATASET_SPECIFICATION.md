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
count, not a new clip.

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
| `rules` | `{children, staff}`: `count` or `exclude`, as chosen for the validation (spec section 4) |
| `conditions` | `{traffic, traffic_per_camera_hour, lighting, occlusion}` (section 3) |
| `tags` | scenario tags chosen by the person who counted (groups, people stopping, ...) |
| `notes` | free text |
| `reviews` | one or two independent counts: `{reviewer, at, run, video, marked, watched_pct, crossings: [{camera, t, clock, direction}]}` |
| `created_at`, `updated_at`, `made_with` | when, and which CrossingCount version |

Only counts that meet all of these are accepted: counted by hand in the wizard's Manual
mode; at least 99% of every counted camera's footage watched; clean footage (not the
sensor's marked footage); the footage's clock known; store code and the counter's name
given. A second count must cover the same cameras and traffic, under the same rules.

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
  request, and every scoring of it is recorded. Tuning on it makes its result worthless.

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
clips: [{id, split, sha256, crossings}]
```

Every scoring records the manifest of what it scored: the frozen version it matches
exactly, or `unreleased` with its content hash. A frozen version can always be checked:
if a listed clip's file no longer has the listed hash, the check says so.

## 6. Checks

The gold page runs these every time it opens:

- every clip file reads and has schema `gold/1`;
- every clip's set is its store's set, and no store is in two sets;
- every frozen manifest still matches the clip files it lists.
