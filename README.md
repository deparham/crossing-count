# crossing-count

Assisted, human-verified crossing counts for validating overhead people-counting
sensors (RetailNext and similar) against CCTV footage.

This tool never reports an automatic count. It proposes candidate crossings; a
person accepts, rejects or splits each one, and only the accepted set is ground
truth. An automatic detector cannot be ground truth for a sensor claiming higher
accuracy, and its errors would correlate with the sensor's.

Everything runs locally. Nothing is uploaded, and there is no telemetry. Footage
is read where it lies and never copied into the repo (`.gitignore` blocks video
files, and `runs/`, which holds images derived from footage). The one network
use is optional: fetching RetailNext's own counts from its API (`retailnext.py`),
which only downloads numbers.

## Status

| Milestone | What | State |
|---|---|---|
| M1 | Motion gating (`gate.py`) and camera tracing (`trace_line.py`) | built |
| M2 | Candidate crossings (`detect.py`: detection, tracking, counting rule) | built |
| M3 | Review page (`review.py`) | built |
| M4 | CSV export, pipeline metrics, report (`export.py`) | built |
| – | Manual counting page (`count.py`), no detection at all | built |
| – | Count wizard (`wizard.py`): automatic or manual count, then a PowerPoint report | built |
| – | Windows installer (PyInstaller + Inno Setup, built by GitHub Actions) | built on GitHub |
| – | Benchmark (`bench.py`): what the automatic count finds, on checked clips | built |
| – | RetailNext API (`retailnext.py`): connect, list locations, fetch traffic | connect and fetch built; reading the answer into the wizard waits for a real answer |

## Setup

```bash
brew install uv
cd crossing-count
uv sync
```

## Easiest: the count wizard (`wizard.py`)

```bash
uv run wizard.py
```

A page opens in your browser. Pick the footage (Downloads, Desktop, Movies and
Videos are listed), with or without the sensor's marks, then choose how to count.

**Automatic.** The tool counts while you do other work, and then you check it.

1. **Cameras.** The setup page opens for each camera. Cameras you drew before
   are found by themselves: by their burned-in line, or, for clean footage,
   because they were drawn on the same video.
2. **Traffic.** Choose In, Out or both.
3. **RetailNext.** Enter the sensor's number for the same cameras and period.
   For footage longer than one 15-minute interval, enter RetailNext's number
   for each interval, as in its table; each camera's own number is optional.
   The report then gets a page comparing them interval by interval (with a
   chart) and camera by camera, noting partly covered intervals, the largest
   difference, and cameras that overlap. Once connected to RetailNext
   (`retailnext.py connect`), **Fetch from RetailNext** fills all of this in
   from each camera's entrance in RetailNext, and warns about intervals
   RetailNext marked incomplete or imputed; the report says the numbers came
   from its API (or that they were typed in, if you changed them).
4. **Count.** `gate.py` and `detect.py` run in the background. The page shows
   what they are doing: a live picture, the progress and the time left.
5. **Check.** Each crossing the tool found plays as a short loop. Count
   everyone who crosses in it: `Y` for one person, `2`–`5` when a group
   crosses together, `N` for nobody, `U` when you cannot tell. The likely
   misses it listed come next, answered the same way.
   Finally, you can watch the movement it could not explain, at 2× speed, and
   press `I` or `O` for anyone it never detected. Do this on busy entrances.
   If two cameras see the same person at the same moment, the second crossing
   is marked, so that person is counted once.

**Manual.** You count every person yourself.

1. **Cameras.** If the video shows RetailNext's light-blue line, you only name
   the cameras. If it is clean, you draw the line first, and it is shown on
   the video while you count.
2. **Traffic.** Choose In, Out or both.
3. **Count by hand.** Play each camera at 0.5× to 16× and press `I` or `O` as
   each person crosses; `Z` undoes. A timeline shows the stretches you have
   watched and every count. If you finish with stretches unwatched, the page
   asks you to confirm.
4. **RetailNext.** Enter the sensor's number.

Both end with the **report**: an A4-portrait PowerPoint in the layout of iTOi's
camera validation report. The first page has the count, the system count, the
accuracy, and a picture of the busiest moment. The pages after it list every
crossing with its time and show a snapshot of each one. The count is labelled
VERIFIED COUNT (automatic, then checked by a person) or MANUAL COUNT. The logo
comes from `assets/logo.png`.

The automatic report's number holds only crossings a person confirmed. When
the check is not complete, the accuracy card says **INCOMPLETE** instead of a
percentage, with the reason under the cards: some of the movement the tool
could not explain was not watched (automatic), or less than 99% of a camera's
footage was watched (manual). Anyone in that footage may be missing from the
count. Crossings answered Unsure are never counted: the report lists them,
and gives the count and the accuracy both ways they could go.

A question is about a moment, not one person. When a group crosses and the
tool counts only one of them, the others are not asked about separately, and
their movement is not among the stretches to watch: answer with how many
crossed (`2`–`5`). On one busy clip, answering `Y` alone halved the count.

**Learning examples.** Set a shared folder on the report step. Every counted
crossing is then saved there, as the camera's frames from 1.5 s before to
1 s after it. With the frames goes a description, `meta.json`, with the camera,
time and direction, the counting line, and who counted. Checks answered "no"
and random watched moments with nobody crossing are saved the same way, as
negatives, and `index.jsonl` lists everything. This is the data for measuring
the automatic counter on your own cameras and, once there is enough, for
retraining it. The examples are CCTV frames of people, so keep the folder
inside the company.

Everything is saved as you go, to `runs/<video>/wizard/state.json`. Open the
same video again to carry on where you left off.

Each count also records which detector (with its settings) and which version
of CrossingCount proposed the crossings, and how long it took against the
length of the footage; the report says so in its method section.

Every answer, undo, added crossing and watching decision is logged there with
the time and the checker's name (asked once, then remembered). Running a
checked count again asks first: the check starts afresh, and the old one, with
a copy of its report, is kept in `runs/<video>/wizard/history/`.

## Training a head detector (`label.py`, `train_heads.py`)

The current detector learned from side-on photos, so from overhead it loses
people in crowds. To train one on your own cameras:

1. Open **Mark heads** from the count wizard (or run `uv run label.py`) and
   add frames from your videos. It picks about 30 moments per camera, mostly
   the busiest.
2. Put a circle on every head. Where a video was counted with the wizard, the
   detector's fairly sure people (25% or more) come already circled in yellow,
   at the far end of each body; drag, delete or add circles as needed. The
   guesses are rough: on a busy picture about half need moving or deleting,
   and people away from the counting line get no guess at all.
3. Aim for about 1000 heads, from several stores, busy and quiet, with and
   without the sensor's marks. That takes roughly 2 hours.
4. `uv run train_heads.py` trains on this computer. It then reports how many
   heads the new detector and the current one each find, miss and invent, on
   marked frames it never trained on. The new weights go to `models/heads.pt`
   in the data folder, and nothing counts with them until they prove better
   on checked clips.

The marked frames are camera pictures of people, so they stay in `labels/` in
the data folder, which git ignores.

A first try (1,009 heads on 180 pictures, 2026-09-13) did not beat the current
detector: on a store it never saw it found 28 of 234 heads, where the current
one found 166. Most frames were marked footage, and it learned the sensor's
height bubbles rather than heads. It is not used.

## Measuring the automatic count (`bench.py`)

    uv run bench.py                # every clip with a finished count by a person
    uv run bench.py VIDEO [VIDEO]  # just these

For each clip it replays the recorded detections through today's tracking and
counting rule (seconds per clip, nothing in the run folder changes) and puts
every crossing the person verified where the checker would meet it:

- **counted**: the tool counted it (a Y/N question);
- **on the list**: offered as a possible miss (a Y/N question);
- **in a loop**: not asked about, but crossing inside a question's loop, so
  counted when the checker answers with the number of people;
- **by watching**: only inside movement the tool could not explain;
- **never shown**: nowhere the tool pointed.

It also counts wrong counts (the same person twice, the wrong direction,
nobody) and the work: Y/N questions and minutes of movement to watch.
Results are saved in `bench/` in the data folder, to compare later changes.

Only a clip counted fully by hand (the wizard's Manual mode, with all the
footage watched) measures **crossing recall**. A check of the tool's own
output cannot contain someone the tool never showed, so on those clips
"never shown" only means "shown when the clip was checked, but not by
today's method". A hand count of part of the footage is shown but left out of
the totals. If a camera's drawing changed since its count ran, the clip is
skipped unless you pass `--allow-config-change`.

## RetailNext's numbers from its API (`retailnext.py`, optional)

    uv run retailnext.py connect      # add a subscription: name, access key, secret key
    uv run retailnext.py list         # the connected subscriptions
    uv run retailnext.py check        # test every connected key
    uv run retailnext.py locations    # the stores (and other locations) the keys see
    uv run retailnext.py traffic VIDEO  # RetailNext's 15-minute traffic for that period

Connect each customer's subscription once; they all stay connected, and the
app uses whichever one has the store you pick, so you never switch. A store
code two customers share is typed as `subscription/code` (e.g. `rag/CN-123`).
`connect rag` re-adds a subscription whose key is already on this computer
without typing it; `forget rag` removes one.

A RetailNext admin makes the key under Admin Settings > System Access Tokens;
a key limited to Data, to the stores you validate, and with an expiry date is
enough. `connect` asks for it in your terminal and keeps it in this computer's
credential store (Keychain on a Mac, Credential Manager on Windows), never in
a file, a report, a log or git; only the subscription name goes in
`settings.json`. `forget` removes it. The only requests are RetailNext data
queries: nothing about the footage is sent. Answers are kept as they came in
`retailnext/` in the data folder.

**Getting footage.** Once connected, the wizard's first page has *Get footage
from RetailNext*: pick the store, the day, the length (15 minutes to an hour),
the traffic you validate and how you will count. It asks RetailNext for that
day's 15-minute traffic over opening hours and shows the busiest windows for
that traffic (most out for an out validation, most in for an in one), on
RetailNext's own 15-minute boundaries so the comparison is exact. Pick one and
it exports every camera of the store for that window, clean for an automatic
count and with RetailNext's marks for a count by hand, downloads it next to
your other footage (named like RetailNext's own exports, never over another
file), and opens it with those choices made. Exports are jobs on your
RetailNext account, which it deletes after 7 days. The key goes only to
RetailNext: redirects are never followed, and the download link gets no key.

`traffic` asks in the store's own time zone (from RetailNext's location list)
and shows each 15-minute interval's in, out and validity. RetailNext marks an
interval incomplete or imputed when its data had a problem (a sensor without
power, say); such an interval is not a fair comparison. The API can also
export video for chosen channels and times, with or without RetailNext's
overlay; using that to fetch clean footage is a possible next step.

## Windows app

The app installs on Windows as a normal program, with a Start-menu and a
desktop shortcut, and needs no Python. It opens the count wizard in the browser
and keeps a small window open while it runs; closing that window quits it.

GitHub's Windows machines build the installer, with
`.github/workflows/windows-installer.yml`:

1. Create a **private** GitHub repository and push this project to it. Footage,
   runs and model weights are never committed (see `.gitignore`).
2. Every push to `main` or `master`, or a tag such as `v1.0.0`, builds
   `CrossingCount-Setup-<version>.exe`. Download it from the workflow run's
   **Artifacts**.
3. The workflow downloads the same YOLO weights as on the development Mac and
   checks their SHA-256. It bundles everything with PyInstaller
   (`packaging/crossing_count.spec`), runs the program's own self-test, and
   wraps the result with Inno Setup (`packaging/installer.iss`). A separate
   job runs the tests on Windows.

Once installed, the program files are read-only. Your drawings, runs and
settings live in `%LOCALAPPDATA%\CrossingCount`, and the installer starts you
off with the camera drawings in `sites/`. Automatic counting runs on the
processor, so no graphics card is needed, but it is slower than on a recent
Mac. Manual counting is unaffected.

To build the program folder yourself, on a Mac or on Windows:

```bash
uv run --group build pyinstaller packaging/crossing_count.spec --noconfirm
```

On a Mac this folder is about 950 MB; `dist/CrossingCount/CrossingCount --self-test`
checks that it loads everything.

Licences: YOLO (Ultralytics) is under AGPL-3.0. Check with whoever handles
licensing before giving the program to anyone outside the company: that needs
Ultralytics' commercial licence or AGPL compliance.

## Quick path: count by hand (`count.py`)

The simplest way to check a sensor: you do all the counting, and the tool
records it. No detection or tracking is involved, and no camera setup is needed.

```bash
uv run count.py "Export - Multiple Channels - 2026-09-12-113000 AEST to 2026-09-12-114500 AEST.mp4" --operator "Your Name"
```

The page plays one camera at a time, cropped out of the multi-camera export.
Watch the light-blue counting line, and press `I` when someone goes in and `O`
when someone goes out. Other keys:

- `Z` undoes the last count on this camera.
- `Space` plays and pauses. The arrow keys step 2 s (0.1 s with `⇧`); up and
  down change the speed.
- `1`–`4` switch camera. Each camera resumes where you left it.

Under the picture, a timeline shows the stretches you have watched and every
count. The report lists anything left unwatched, so a count is only complete
when a camera shows 100% watched.

Type the sensor's numbers from RetailNext into "Compare with the sensor". Enter
them per camera, or as the combined figure. Accuracy shows at once, per
15-minute interval. An interval the video only partly covers is flagged,
because the sensor's figure for the whole interval is not comparable.

Everything is saved on every key press to `runs/<video>/manual/counts.json`.
**Save report and CSV** writes one CSV per camera (the five columns below,
after a header block) plus `report.html` and `summary.json`, to
`runs/<video>/manual/export/`. `uv run count.py VIDEO --export` does the same
without opening the page.

The clock comes from the RetailNext filename. For other names pass
`--start "2026-09-12 11:30:00"`. Camera names come from `gate.py` if it ran,
else from `--cameras "CN-123-PB1,CN-123-R2"` or the page.

The rest of this README covers the assisted path: the tool proposes crossings
and a person confirms them.

## 1. Set up each camera once

The easiest way is the setup page, which opens in your browser. It is served
from this Mac only (127.0.0.1) and loads nothing from the internet.

```bash
uv run setup_ui.py "/path/to/Export - Multiple Channels - ....mp4"
```

Each camera picture in the video gets a tab. Cameras already saved in `sites/`
open in their own tab automatically, matched by their burned-in line.

- **Draw:** pick a tool on the right and click on the picture: counting line,
  inside side, mask zone, filter zones, exclusion zones.
- **Adjust:** use **Move points** to drag a point, or **Alt**-click a point to
  delete it.
- **Check:** the Check box updates as you draw. It shows how much of your line
  sits on the burned-in line, the counting rule that applies, and any problem,
  such as a mask on the outside of the line or a zone whose corners were
  clicked out of order.
- **Save:** writes `sites/<camera>.json`.
- **Busy scenes:** untick **People-free picture** to see a live frame from
  anywhere in the video, which helps find a band hidden behind people.
- **Port in use:** if the page reports the address is already in use, it is
  already running; open http://127.0.0.1:8765, or add `--port 8766`.

The older click-in-a-window tool still works:

```bash
uv run trace_line.py "/path/to/Export - Multiple Channels - ....mp4" --site CN-146 --sensor CN-146-PB2
```

For a multi-camera export you first click the camera picture to trace. You then
click, in order:

1. **Counting line**: along the burned-in light-blue line with triangles, one
   point per bend.
2. **Inside**: one click on the store side (the side the triangles point to).
3. **Mask zone**: the grey band. Press `S` if the camera has none.
4. **Filter zone(s)**: the light-blue outline. `N` starts another, `S` if none.
5. **Exclusion zone(s)** (optional): crossings touching them are flagged for the
   reviewer, never dropped.
6. **Gate-ignore zone(s)** (optional, rare): motion that is never a person, such
   as screens or doors.

The picture shown is a median of frames sampled across the video, so people are
removed and the overlay is easy to follow (`F` shows a live frame). The result is
`sites/<sensor>.json`, plus a preview image under `runs/trace_previews/`. To
adjust an existing camera, use `--edit sites/<sensor>.json`. Never hand-edit
coordinates.

What a camera has decides its counting rule, which mirrors how the sensor counts:

| Camera has | A crossing counts when |
|---|---|
| line only | always (a U-turn is one in plus one out) |
| line + mask zone | the person's track dwells in the mask zone (`min_dwell_in_zone_s`) |
| line + filter zone | the person's track touches a filter zone somewhere on its path |
| line + both | both |

Coordinates are normalised to the camera's own picture, so a config works
whatever layout the camera appears in. Tracing records the colour of the
burned-in line. Later stages use it to find which picture belongs to which
camera, and to warn if the line in the video no longer matches the config (for
example, the sensor was reconfigured).

## 2. Gate the video (M1)

```bash
uv run gate.py VIDEO sites/cn-146-pb2.json sites/cn-146-l1.json --debug
```

Add `--sample 120` to process only the first two minutes. For each camera it
writes `runs/<video>/<sensor>/activity.json`, the time ranges with motion near
the counting line, and reports the share of runtime eliminated. With `--debug`
it also writes images to check the gate by eye:

- `gate_region.png`: the camera geometry and gate region over a people-free frame.
- `timeline.png`: motion over time, with the kept ranges shaded.
- `eliminated_closest_calls.jpg`: the highest-motion moments the gate dropped.
  **Look at this one.** If a person near the line appears here, the gate is too
  strict.
- `eliminated_random.jpg` and `active_random.jpg`: random samples of each.

The gate is biased toward keeping time. A missed person is unrecoverable; an
empty second costs a little compute. It uses MOG2 background subtraction seeded
from a median frame, a generous band around the line plus the mask zone, a
small minimum blob size, and padding and merging of ranges.

## 3. Propose candidate crossings (M2)

```bash
uv run detect.py VIDEO sites/cn-146-pb2.json sites/cn-146-l1.json --debug
```

Run `gate.py` first, with the same video, configs and `--sample`. `detect.py`
detects and tracks people only inside the gate's active ranges, applies each
camera's counting rule, and writes three files per camera:

- **`candidates.json`**: every crossing the rule would count, for a person to
  accept, reject or split. Each one has its time (stamped at the line), direction,
  confidence, box, clip window, path, where along the line it crossed
  (`line_position`, 0 to 1) and how many tracks were nearby (`concurrent_tracks`).
  The last two are for diagnosis: misses clustered at one end of the line point
  to occlusion or coverage; misses at high concurrency point to the sensor
  merging groups.
- **`discarded.json`**: every track the rule rejected, with its reason (below).
- **`unexplained.json`**: motion near the line that produced no proposal. These
  are the likely misses, and the reviewer sees all of them.

### The counting rule

Each person's track is judged as a whole:

1. A crossing followed by one in the opposite direction on the same track
   cancels. The person went out and came back, or in and back out. **Neither
   counts, and the pair is reported** (`uturn_no_mask` if they never reached the
   mask zone, otherwise `returned_same_track`). To count every crossing instead,
   set `"same_track_returns": "count"` in the camera's config.
2. A remaining inward crossing needs the track to stay in the mask zone for
   `min_dwell_in_zone_s`, within `pending_timeout_s` of crossing. A remaining
   outward crossing needs the track to have stayed in the mask zone before
   crossing. Both apply only if the camera has a mask zone.
3. If the camera has filter zones, the track must touch one somewhere on its path.

Counts are timestamped at the line crossing, not when the rule is satisfied, so
they land in the same 15-minute interval as the sensor's.

| Discard reason | Meaning |
|---|---|
| `uturn_no_mask` | crossed in, came back out without reaching the mask zone |
| `returned_same_track` | out and back in, or in (past the mask zone) and back out, on one track |
| `pending_expired` | crossed in, then lost or timed out before the mask zone. **Undercount indicator.** |
| `pending_at_eof` / `pending_at_range_end` | still waiting when the footage or the gate range ended |
| `outward_no_mask` | crossed out without having been in the mask zone |
| `no_filter` | never touched a filter zone |
| `duplicate` | the same direction at the same spot moments after another count, on a split track (`duplicate_of` names the one kept) |

If `pending_expired` exceeds 2% of committed counts, `detect.py` prints a
warning. At that point the mask rule is costing more than the U-turns it
removes, most likely through occlusion between the line and the zone.

Flags on candidates are all for the reviewer, never for dropping a candidate:

- `exclusion_zone`: the track touched an exclusion zone.
- `stitched`: the track was joined across a short gap.
- `track_also_returned`: the same track also went out and back (or in and back).
- `possible_return`: an opposite count on another track, close in time and
  place; perhaps one person whose track broke.
- `crossed_while_unseen`: the tracker bridged a gap of more than 0.5 s across
  the line, so nobody saw the actual crossing.
- `big_jump_near_line`: within 3 s of the crossing the track moved more than a
  fifth of the picture height in one step. Two people may have been glued
  into one track, so check whether one person really crossed.

### Busy scenes

With many people in view, trackers swap identities and displays near the line
make people linger on it. Several safeguards address this:

- **Cutting at jumps.** A track is cut wherever it moves further than a person
  can walk between two samples (0.08 picture heights plus 0.35 heights per
  second), because that is a swap, not a walk. A tighter limit lost real
  crossings on a busy test clip, so smaller jumps near the line are flagged
  as `big_jump_near_line` instead of cut.
- **Walking-speed joining.** Broken tracks are only joined if the gap could be
  covered at walking speed.
- **Tracking boxes.** The tracker associates people by their full box by
  default. `assoc_box="compact"` (a small box on the lower body) was tried for
  crowds, but on real recorded detections it broke tracks two to three times
  more often, so it is not the default.
- **At-the-line threshold.** A change of side only counts once the person is
  clearly past the line, about 3% of the picture height.
- **Duplicate merging.** Same-direction counts moments apart at the same spot
  are merged; the extra one goes to the discards as `duplicate`.
- **Likely misses first.** A track that stops just short of the line, followed
  by one that starts just past it, is listed first in `unexplained.json` as
  `broken_track_at_line`, with a guessed direction. These are the most likely
  missed crossings, and `proposed.py` prints them.

### Fisheye

People away from the lens appear lying along the radius, head outward. By
default the detector runs on overlapping crops, each rotated so people stand
upright, then maps results back to the video's own pixels; the frame is never
dewarped. `--naive` runs the detector on the raw picture instead, and
`--compare` runs both on the same clip and prints them side by side.

Anything detected in the people-free median frame (clothing racks, mannequins)
is treated as static and ignored when later detections match it closely.

Tracks have no long-term identity. A track that dies and is followed within
`stitch_gap_max_s` by one nearby, moving the same way, is joined to it.

Weights live in `models/` and are never downloaded at run time. One-time setup:

```bash
mkdir -p models && curl -L -o models/yolo11s.pt https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s.pt
```

Ultralytics is forced offline and its usage analytics are switched off. Its
licence is AGPL-3.0, which is fine for internal use; distributing this tool
would need Ultralytics' commercial licence.

## 4. See the proposed numbers

```bash
uv run proposed.py "/path/to/Export - Multiple Channels - ....mp4"
```

For each camera, and in total, this prints:

- the proposed ins and outs, each with its clock time (video 00:00:00 is read
  from the export filename; override with `--start "2026-09-12 11:30:00"`);
- the tracks the rule rejected;
- the stretches of unexplained motion.

These are **proposals, not verified counts**. On real footage, some true
crossings were only in the rejected or unexplained lists, so the review below
covers those too.

## 5. Review (M3)

```bash
uv run review.py "/path/to/Export - Multiple Channels - ....mp4" --operator "Your Name"
```

The review page opens in your browser and plays each item as a short clip,
cropped to its camera, with the line, zones and the person's path drawn on. It
goes through, per camera:

- **every proposed crossing:** `A` accept, `R` reject, `S` split into two
  people, `U` unsure, `F` flip the direction;
- **every lost entry and duplicate, plus a random quarter of the other
  rejections:** `C` the rule was right, `X` restore as a real crossing;
- **every stretch of unexplained motion, likely misses first, played at 2×:**
  press `I` or `O` the moment someone goes in or out, then `Enter` when done.

Other keys:

- `1`–`5` tag the item: child, staff, pram/trolley, group merge, unsure.
- `Space`, `←`/`→` and `↑`/`↓` control playback.
- `Z` removes the last crossing you added to the current item; with none, it
  undoes the last decision. A held key acts once, and adding a second crossing
  in the same direction at the same moment asks you to confirm it was two people.
- `N` and `P` move to the next or previous item without deciding.

Every key press is saved to `runs/<video>/review/decisions.json`, including
unfinished work on an item. Close the page any time and it resumes where you
stopped. The header shows progress and the time left.

## 6. Export (M4)

```bash
uv run export.py "/path/to/Export - Multiple Channels - ....mp4" --sensor-in 20 --sensor-out 23
```

This writes to `runs/<video>/export/`:

- **`<camera>.csv`:** the manual tool's layout. First a header block (site,
  sensor, video, the clock time of video 00:00:00, operator, the counting rules
  including the mask zone), then the columns
  `video_time,video_seconds,clock_time,direction,tags`.
- **`pipeline_metrics.json`:** the pipeline's own error rates, measured by the
  review. It covers proposals accepted, rejected and split; unexplained
  stretches reviewed and how many held a missed crossing; U-turns and returns
  discarded; lost entries; stitched tracks.
- **`report.html`:** verified in and out per camera and in total, per 15-minute
  interval, tags, the pipeline's error rates, and the sensor's accuracy when you
  give its numbers.

To give the sensor's numbers, use `--sensor-in` / `--sensor-out` for combined
counts, or `--sensor CAMERA:in=N,out=M` for each camera.

Until the review is finished, the counts can only go up, and every output says
so.

## Timing

All times come from the video's frame timestamps, never `frame / fps`. Every
run checks:

- that `fps_assumed` in the config matches the footage;
- for gaps in the timestamps;
- that the video's length matches the interval in an NVR export filename
  (`... 2026-09-12-125627 AEST to 2026-09-12-130127 AEST.mp4`). A shortfall means
  frames were dropped before export, so video time drifts from wall-clock time.

## Known limitations

- The sensor's own tracks and height labels are burned into RetailNext exports
  and cannot be hidden. A reviewer who can see the sensor's opinion may be pulled
  toward it, and the review stage cannot remove those pixels.
- Busy entrances cost review time. On one 15-minute two-camera clip with 11-13
  people in view, the motion gate removed almost nothing. Unexplained ranges
  covered about 80% of the footage, and the queue came to 190 items, estimated
  at 24 minutes of review. The tracker also broke tracks at the line often, so
  many crossings appear only in the likely-miss list, not as proposals. On quiet
  clips the gate and the proposals save far more time.
- Multi-camera layout detection has been exercised on a RetailNext two-camera
  export and on synthetic grids. A real 2x2 export with header bars has not been
  seen yet.
- On macOS, OpenCV and PyAV each bundle FFmpeg's camera-capture classes, and the
  Objective-C runtime prints a harmless `Class AVF... is implemented in both`
  warning at start-up. The tool never uses a camera.

## Tests

```bash
uv run pytest
```

The tests use synthetic videos only.
