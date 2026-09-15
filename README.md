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
| – | Mac app (`CrossingCount.app` in a `.dmg`, built by GitHub Actions) | built on GitHub |
| – | Benchmark (`bench.py`): what the automatic count finds, on checked clips | built |
| – | RetailNext API (`retailnext.py`): connect, list locations, fetch traffic | connect and fetch built; reading the answer into the wizard waits for a real answer |

## Documents

- [Production audit](docs/PRODUCTION_AUDIT.md): architecture, strengths, risks, the plan.
- [Ground Truth Specification v1.0](docs/GROUND_TRUTH_SPECIFICATION.md): what a crossing
  is, for the people who count.
- [Dataset specification](docs/DATASET_SPECIFICATION.md): the gold set, its sets and
  versions.
- [Metrics specification](docs/METRICS_SPECIFICATION.md): every number, its formula,
  edge cases and uncertainty.
- [Sensor data](docs/SENSOR_DATA.md): getting a counting system's numbers in (RetailNext,
  or a CSV from any counter).

## Setup

```bash
brew install uv
cd crossing-count
uv sync
```

## Opening and closing it (no terminal needed)

On the Mac, double-click **CrossingCount** in your Applications folder (drag it
to the Dock for one click). It opens in its own window, not a browser tab
(`uv run wizard.py --browser` gives the old browser tab). Closing the window,
or **Quit** at the top of the page, closes it, and everything is saved as you
go. On a Mac without
this project, install the Mac app (below). On this one,
`packaging/mac/build_launcher.sh` builds a launcher that runs this folder. RetailNext
brands are connected and removed on the first page too: type the brand, and
if it is not connected yet, enter its API key there.

**Updating.** When a newer version is on GitHub (or one on this Mac that is not
running yet), an **Update** button appears at the top of the page. It lists
what is new, takes the new version only by fast-forward, starts the app again
and reloads the page; it waits while a count or download is running, and
changes nothing if the update cannot be applied cleanly. Your camera drawings
in `sites/`, runs, reports and settings are not in git, so an update never
touches them. (The Windows app is updated with a new installer instead.)

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
   each person crosses; `Z` undoes. `⇧I` / `⇧O` count a crossing you are not
   sure of: it is listed in the report but never counted. `U` marks the last
   crossing uncertain (or not), `E` opens it to change its direction, move it
   to the paused moment or add a note, and `,` / `.` step one frame. Every
   crossing in the list has an edit link too; each correction asks for a
   reason and is kept with what it was. A timeline shows the stretches you
   have watched and every count (uncertain ones in yellow). If you finish
   with stretches unwatched, the page asks you to confirm.
   When a gold clip has been counted by two people, the moments they disagree
   on are listed beside the video: **Show** jumps to each, and a third person
   settles it (In, Out, no crossing, still uncertain) with a reason. Both
   counts stay as they were.
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

**Too few crossings for a percentage.** Below 30 verified crossings the
report prints no percentage: one crossing would move it by several points.
The card says DIFFERENCE instead, with the counts beside it ("verified 11,
RetailNext counted 9: an undercount of 2"); the per-interval and per-camera
rows do the same. To get a percentage, finalise several windows of the store
and put them together on the *Validation runs* page (an engagement): with at
least 30 verified crossings it gives the percentage, and with at least 5
windows its 95% range.

A question is about a moment, not one person. When a group crosses and the
tool counts only one of them, the others are not asked about separately, and
their movement is not among the stretches to watch: answer with how many
crossed (`2`–`5`). On one busy clip, answering `Y` alone halved the count.

**The team's shared folder.** Set a shared folder on the report step: a
SharePoint library synced by OneDrive (its "Sync" button), or a network share.
Everyone's work then lands in one place:

- **Learning examples.** Every counted crossing, as the camera's frames from
  1.5 s before to 1 s after it, with `meta.json` (camera, time, direction, the
  counting line, who counted, the store's set). Checks answered "no" and random
  watched moments with nobody crossing are saved the same way, as negatives;
  `index.jsonl` lists everything.
- **The validation's record** (`validations/<store>/<video>.json`): answers,
  counts by hand, the sensor's numbers, what counted as a person, the decision
  log. No video.
- **Gold clips** (`gold/gold_v1/`), which every computer then includes.

Every example names its store's set, and those from test-set stores say
`train_ok: false`: never train on them, or the test results mean nothing.
Training is deliberately a step a person starts, never automatic: a new model is
used only if it scores better on the gold development set. The folder holds CCTV
frames of people: check your agreements with the retailers allow it, and keep
it to your team.

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

## Finalised validations and the audit log

**Finalise** on the report page gives a validation an ID, `CC-VAL-<year>-<computer>-<number>`
(the computer's four characters keep a team's IDs apart), and keeps it in
`validations/<ID>/` in the data folder, never changed again:

- `manifest.json`: everything needed to reproduce and trust the result: CrossingCount's
  and its libraries' versions, the engines' versions, the Ground Truth Specification
  version and what counted as a person, the footage's name, fingerprint and SHA-256 and
  its clock, each camera's line and mask, the system's numbers with their source and a
  checksum, the detector, its weights' SHA-256 and settings (automatic counts), the
  computer (OS, processor, memory, GPU), the audit log's latest hash, and the SHA-256 of
  every other file in the folder;
- `result.json`, `intervals.csv` (verified against the system, interval by interval),
  `crossings.csv` (every verified crossing, and every unsure one marked not counted),
  `decisions.json` and the report as it was made.

The files are read-only, and the **Validation runs** page (header) checks every one
against its manifest each time it opens. A finalised validation refuses any change; to
correct it, **Start a new version**: the finalised one stays as it was, and the new one
gets its own ID when finalised, naming the one it replaces. With a shared folder set,
finalised runs are copied to `<shared>/runs/<ID>/`.

**The audit log** (`audit/audit.jsonl`) records every meaningful action: answers, crossings
added and removed, counts by hand, the traffic, cameras and store details, the rules, the
system's numbers, reports made, finalising and new versions: when, who (the name typed and
the computer's login), on which footage, the value before and after. Each entry carries
the SHA-256 of the one before, so a changed, removed or inserted entry breaks the chain,
and the Validation runs page says where. Names are typed, not accounts: the log shows what
was done under which name, not proof of who sat at the computer.

## Sensor results: how far the counting system is from the truth

Most people counters, RetailNext among them, give a count per interval, not the
individual crossings, so a system under test is judged by its **count error**,
15-minute interval by 15-minute interval (never by precision or recall, which need
individual crossings). The wizard's system step takes the numbers from RetailNext's
API, from a CSV exported by any other counter (Xovis, V-Count, FootfallCam, a
spreadsheet: format in [docs/SENSOR_DATA.md](docs/SENSOR_DATA.md)), or typed in.

Each report keeps its result as data. The **Sensor results** page (header) puts every
complete validation counted on clean footage together, on this computer and in the
team's shared folder, per counting system:

- bias (net error as a share of the verified count), over- and undercount, MAE and RMSE
  per interval, WAPE (over- and undercounts that do not cancel) and MAPE on busy
  intervals, per direction and together, by store and by traffic level;
- 95% ranges that resample whole validations and whole stores (intervals of one clip are
  not independent), with none below five;
- a sentence that says what was measured, on how much, and how sure: "Across 3 stores,
  12 validations, 6.5 hours of camera footage and 1,480 independently verified
  crossings, RetailNext counted 3.1% too few (95% range ...)".

Definitions in [docs/METRICS_SPECIFICATION.md](docs/METRICS_SPECIFICATION.md).

## Gold set: measuring the automatic count against full hand counts

The question is how reliably CrossingCount finds every real crossing, and only
a count that looked everywhere can answer it. "Every crossing the tool proposed
was checked" is not that: people the tool never showed are missing from such a
check. So:

- **Gold clip.** A count by hand in the wizard (Manual), on clean footage, that
  watched at least 99% of every counted camera's footage can be kept on the
  report page as a gold clip, with its lighting, how often people were hidden,
  tags (groups, people stopping or coming back, ...) and notes; its traffic
  level is worked out from the count. It is saved in `gold/gold_v1/` with the
  store, cameras, period, video facts, what counted as a person, the Ground
  Truth Specification version, every crossing (camera, time, clock, direction),
  who counted and when. Models never write to it. See
  [docs/DATASET_SPECIFICATION.md](docs/DATASET_SPECIFICATION.md).
- **Sets by store, fixed.** A store's set comes from its code (about one store in
  five goes to test, one in five to validation, the rest to train), the same on
  every computer and for good, so no store's footage is on both sides. Tune on
  the development set (train and validation); the test set is for a final
  check, scored only on request, against a frozen version, and every use is
  recorded.
- **Versions.** "Freeze this version" on the gold page writes a manifest that
  lists every clip with its checksum and keeps the clips themselves; it is never
  rewritten. Every score names the version it used.
- **A second person.** "Count it again as someone else" starts a blind second
  count of the same footage (the first is kept). The two are matched like the
  tool's crossings; what both counted is the clip's truth, and the moments
  they disagree on are listed and left out of the scoring as uncertain,
  never settled by the tool.
- **Scoring** (the **Gold set** page, from the header). Each clip needs an
  automatic count of the same cameras over the same period (the footage
  without RetailNext's marks, counted automatically); its recorded detections
  are replayed with today's settings and matched by clock time. A tool
  crossing and a person's are one when on the same camera and at most 2 s
  apart, one-to-one, the most pairs possible (close crossings included):

  | | |
  |---|---|
  | found | counted, the right way |
  | wrong way | counted, the other direction (reported, never hidden) |
  | missed | not counted at all |
  | false | counted with nobody crossing (duplicates are shown within it) |
  | recall | found ÷ the person's crossings (per direction too) |
  | miss rate | missed ÷ the person's crossings |
  | precision, F1 | found ÷ the tool's crossings; their harmonic mean |

  Recall, wrong way and miss rate add to 100%. Rates come with 95% intervals,
  per direction, per set and per tag, and none is given on fewer than 30
  crossings ("insufficient sample"). The page also shows the reviewing the
  tool asks for per camera-hour: questions, possible misses, minutes of
  movement to watch, and how many of the tool's misses its list showed.
- **Records.** Every scoring is kept in `bench/experiments/` with the app
  version, detector and its weights' SHA-256, settings, dataset, set and clips.

**Sensor accuracy is not AI accuracy.** The report's accuracy card compares
RetailNext's count with the verified count: how close the sensor came. The
report now says so, and lists its validation checks (each crossing checked,
each possible miss checked, unexplained movement watched, footage watched)
as met or not.

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
from RetailNext*: pick the brand (subscription; the store list then shows only
its stores, and the last brand is remembered), the store, the day, the length
(15 minutes to an hour),
the traffic you validate, how you will count, and which windows. It asks
RetailNext for that day's 15-minute traffic over opening hours and picks
windows by a stated rule, on RetailNext's own 15-minute boundaries so the
comparison is exact: by default the busiest windows for that traffic (most
out for an out validation, most in for an in one) and a **control window** of
lower traffic drawn at random, so the results can tell crowding apart from a
sensor that is simply off; or one window from each traffic level; or windows
at random from the trading hours (docs/GROUND_TRUTH_SPECIFICATION.md, section
9). The mode, the seed, every window considered and the one chosen are kept
with the validation, and the report's first page says what its number
describes ("peak trading", "a control window", "this period only"). The
windows are shown by role and traffic level only: RetailNext's numbers stay
hidden until the count is done, so they cannot sway it. Pick one and it exports every camera of the store
for that window without RetailNext's marks (the sensor's own tracks on the
picture can sway a count by hand too; "by hand on RetailNext's marked footage"
is still there for a quick look, but never makes a gold clip), downloads it next to
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
desktop shortcut, and needs no Python. It opens the count wizard in its own
window (shown by Edge WebView2, part of Windows 10 and 11); closing the window
quits it.

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

**"Windows protected your PC".** Windows shows that, or *unknown publisher*,
because the installer is not signed with a code-signing certificate: it cannot
tell who made it. It is not a virus warning, and every unsigned program gets it.
Click **More info → Run anyway**, and check the installer's SHA-256 against the
one in the release notes.
[docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md) has the steps, including
`Unblock-File`, and what the warning costs to remove for good. The build signs
the program and the installer by itself once the repository secrets
`WINDOWS_CERT_PFX` and `WINDOWS_CERT_PASSWORD` hold a certificate; without them
it builds unsigned and says so in the run's log and the release notes.

Once installed, the program files are read-only. Your drawings, runs and
settings live in `%LOCALAPPDATA%\CrossingCount`, and the installer starts you
off with the camera drawings in `sites/`. Automatic counting runs on the
processor, so no graphics card is needed, but it is slower than on a recent
Mac. Manual counting is unaffected.

To build the program yourself, on a Mac or on Windows:

```bash
uv run --group build pyinstaller packaging/crossing_count.spec --noconfirm
```

On a Mac this also makes `dist/CrossingCount.app`, about 950 MB;
`dist/CrossingCount.app/Contents/MacOS/CrossingCount --self-test` checks that it
loads everything.

## Mac app

`CrossingCount.app` is the same program for Macs with Apple silicon (M1 or
later, macOS 12 or newer): Python and the detection models are inside, so it
needs no uv, terminal or project folder. GitHub's Macs build it with
`.github/workflows/mac-app.yml` on every push to `main`, or a tag such as
`v1.0.0`: the tests, the same pinned YOLO weights, PyInstaller, the app's own
self-test, then `CrossingCount-<version>.dmg` under the run's **Artifacts**.

1. Open the `.dmg` and drag **CrossingCount** onto **Applications**.
2. The app is not signed with an Apple Developer ID, so the first time macOS
   refuses to open it. Open **System Settings → Privacy & Security**, scroll to
   the message about CrossingCount and press **Open Anyway**. After that it opens
   normally.
3. Double-click it: the count wizard opens in its own window, with its icon in
   the Dock. The Gold set and head-marking pages open in windows of their own.
   Closing the window (or **Quit** on the page, or ⌘Q) closes the app.

Your drawings, runs, reports and settings live in
`~/Library/Application Support/CrossingCount` (its log in `logs/wizard.log`
there), so a new version never touches them. RetailNext keys stay in the
Keychain.

## Update notices and colleagues' apps

Every build GitHub makes of `main` is also published as a release of the
(private) repository: `mac-b<n>` with the `.dmg`, `win-b<n>` with the
installer; the newest three of each are kept. An installed app knows its own
build (`build.json`, written in by the build) and, while its page is open,
checks GitHub every half hour. When a newer build is ready, a banner lists
what changed, a desktop notice appears (from the Mac's Notification Centre;
in a browser tab, the browser asks once for permission), and **Update** does
the rest: the Mac app downloads the new one,
puts it in its own place and starts again; on Windows the new installer runs
silently (Windows may ask to allow it) and starts the app again. If the Mac
app sits where it cannot replace itself, the new `.dmg` opens for you to drag.

A private repository's releases can only be read with a GitHub token. Make a
fine-grained token (GitHub → Settings → Developer settings → Fine-grained
tokens) with access to this repository only and **Contents: Read-only**, then:

- **Build it into the apps** (no colleague types anything): in the project
  folder's page, **Colleagues' apps** → paste it → **Build this token into the
  apps**. Or type it once in an installed app's **Get update notices**.
- **RetailNext brands** go into the apps the same way: **Colleagues' apps** →
  **Build this computer's brands into the apps** sends the brands in this
  Mac's Keychain to the repository's secrets (`RETAILNEXT_BRANDS`); the next
  builds carry them, and colleagues see those brands' stores with nothing to
  type. A key on a colleague's own computer still comes first.

Both buttons need the GitHub command line (`gh`) signed in, and start new
builds at once. **Anything built into the app can be dug out of it by whoever
has it**: if a laptop is lost, change the keys in RetailNext (or revoke the
token on GitHub) and build again. The project folder keeps its own **Update**
button (git) and gets the same banner and notice.

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
  and cannot be hidden, so counting is done on clean exports instead. The tool
  checks each picture itself rather than trusting an answer: marked footage is
  refused for gold clips, and any result counted or checked on it is left out of
  the comparison with the sensor, with the reason
  (docs/GROUND_TRUTH_SPECIFICATION.md, section 10). Earlier results can be
  audited for this on the *Validation runs* page.
- On clean footage there is no burned-in line to draw over, so a line drawn by
  eye may not be where the sensor counts, and part of any difference would be
  the line rather than the sensor. Each drawing records how it was placed:
  calibrated on RetailNext's marked footage (the wizard fetches a minute of the
  same window with marks to draw on) or drawn by eye, and every report says
  which. RetailNext's documented API does not give the line's coordinates, which
  would be the definitive fix.
- Matching a drawing to a camera picture on clean footage uses the picture it
  was drawn on and the camera names of a RetailNext download; the alignment
  thresholds were tuned on a handful of real exports (the same camera a day
  apart scored about 0.6, different cameras below 0.1). A match by name alone
  has to be confirmed by a person before the count runs.
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

## Copyright

Copyright © 2026 Parham Forozan. All rights reserved. See `NOTICE`.
