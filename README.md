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
| M3 | Human check (the wizard's check step) | built |
| M4 | Report, CSV files and finalised validation runs | built |
| – | Counting by hand (the wizard's Count by hand step), no detection at all | built |
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
camera validation report, and the same report as a PDF. The first page is written
for someone who reads nothing else. It opens with the result in words, which way
and by how many people ("In the busiest 15 minutes of 22/08/2026, RetailNext counted
7 fewer people coming in than were verified (41 against 48): an undercount of
14.6%"). Then come the count, the system count and the accuracy, what the sample
describes and how much it rests on, and anything that could not be known (such as
"RetailNext's numbers are the store's total"). Last is the record: the validation
ID (a draft says it is a draft), the sampling mode, the Ground Truth Specification
version, the gold data set's version, and whether the footage was clean or showed
RetailNext's marks. A picture of the busiest moment follows on page 1 when there
is room, otherwise on a page of its own. The pages after it keep the method as
before, list every crossing with its time and show a snapshot of each one. The count is labelled
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

### Counting by hand while the tool counts the clean copy

On the RetailNext step, **Count: By hand on the marked footage, tool on the clean one**
downloads the same window twice: once clean, once with RetailNext's marks. Then, on the
Count by hand step:

- **You count the marked one.** RetailNext's own line is in the picture, so nothing has to
  be drawn by eye, and you can watch its tracker while you count.
- **The tool counts the clean copy**, in the background, on the footage it meets in use.
  Press *Let the tool count it now*; counts run one at a time on this computer.
- **Nothing it finds is shown until you press "I've finished counting."** Seeing it first
  would turn counting into checking its work.
- **Then the two go side by side:** every crossing you counted that the tool did not, every
  one it counted that you did not, and every one it counted the other way. Each has a
  *why?* box — no track, track lost at the line, line somewhere else, counted twice, height
  filter, the camera's view, our own mistake — kept with the validation, for the list of
  things the store can change.
- Differences where **RetailNext counted no more than you did** are marked: counting on its
  marked footage can miss the same people it misses, so those are the ones to look at again.

A count made on marked footage is not independent of RetailNext, so it is kept out of
anything said about RetailNext's accuracy, and a gold clip from it goes in the marked tier
(above). It measures **our** counter, which is what this mode is for.

### Sharing on this network

**Share on this network** (in the header) lets colleagues on the same network use
CrossingCount from their own browser, without installing it. Tick the box and the
panel shows two addresses (this computer's name, such as
`http://Front-Desk-Mac.local:8781/`, and its network address) and an access code. **Copy
link** gives an address with the code in it. From the command line:

```bash
uv run wizard.py --network
```

- **Separate work.** Everyone works on their own validation. Two people cannot open the
  same footage at once; it can be taken over once it has been left for 15 minutes, never
  while it is counting.
- **Counts queue.** Counts run on this computer one at a time, and a waiting one says so.
- **Kept on this computer:** RetailNext keys, updates, settings, the examples folder,
  quitting and "Show the file". People on the network pick footage from its footage
  folders only. They can use its connected RetailNext brands, and so can download footage
  from those stores.
- **Who is on it.** The panel lists everyone using it now, with the footage they have open.
- **Switching off.** It stays on across restarts until switched off. Switching off shuts
  everyone else out at once; their work is saved, and a count already running finishes.
- **The code** is new each time sharing starts.

The first time, the computer may ask whether CrossingCount may accept incoming
connections: allow it. Share only on the company's own network. The connection is plain
HTTP, and everyone with the code sees the footage and reports (see
[docs/SECURITY.md](docs/SECURITY.md), and [docs/LICENSING.md](docs/LICENSING.md) on
Ultralytics' AGPL).

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
  `decisions.json`, and the report as a PowerPoint and a PDF: rendered again from what
  the draft said, with the validation ID on page 1.

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

- **Gold clip.** A count by hand in the wizard (Manual) that watched at least
  99% of every counted camera's footage can be kept on the report page as a
  gold clip, with its lighting, how often people were hidden,
  tags (groups, people stopping or coming back, ...) and notes; its traffic
  level is worked out from the count. It is saved in `gold/gold_v1/` with the
  store, cameras, period, video facts, what counted as a person, the Ground
  Truth Specification version, every crossing (camera, time, clock, direction),
  who counted and when. Models never write to it. See
  [docs/DATASET_SPECIFICATION.md](docs/DATASET_SPECIFICATION.md).
- **Clean and marked footage.** A count on footage showing RetailNext's marks is
  kept as a **marked** clip, because the marks can sway a count towards
  RetailNext's. Marked clips count in the development set's scores, always also on
  a row of their own, and never in the test set or in anything said about
  RetailNext's accuracy. "Clean footage only" on the gold page leaves them out.
  Count a window on both clean and marked footage (another person, or days
  later), and the gold page's "Do RetailNext's marks sway a count?" card shows
  how far the two counts differ and whether the marked one moved towards
  RetailNext's number. It says nothing until 5 windows have been counted both ways.
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
  crossing and a person's are one when on the same camera and at most 3 s
  apart, one-to-one, the most pairs possible (close crossings included). A count
  by hand is a key pressed *after* the crossing is seen — measured at a median
  1.4 s — so the two clocks are lined up first, by the median gap between them,
  and every score says which offset it used:

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

`--tracker` and `--assoc` replay the same recorded detections under a different
tracker or a different association matrix and score the results side by side
(see **Following people between frames** below). Nothing is detected again, and
the run folder is still only read. `--gold development` scores the gold clips
that way instead, at crossing level, keeping each scoring as its own experiment
record:

    uv run bench.py --gold development --tracker byte --tracker ocsort

A scoring made with anything other than the tracking the tool counts with says
so in its own limits, so it can never be read as a result for the product.
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
is still there; a full count on it is kept as a marked gold clip, apart from clean ones), downloads it next to
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

## Quick path: count by hand

The simplest way to check a sensor: you do all the counting, and the tool
records it. No detection or tracking is involved. It is the wizard's **Manual**
mode, described under *The count wizard* above: pick the footage, name or draw
the cameras, then press `I` and `O` at every crossing while a timeline shows
what you have watched. The count, the watched stretches and every correction
are saved as they happen, the report is the same PowerPoint, and a full count can
be kept as a gold clip (on marked footage, as a marked one).

(The standalone `count.py` page that did this was removed on 16 September 2026;
the wizard does everything it did, with a decision log and the gold set. Counts
it left behind are still read by `bench.py`.)

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

**Which detector finds the people.** The crops, the foot point, the
static-object filter, the tracking and the counting rule are the same whichever
detector runs: a detector is only "pictures in, boxes and scores out"
(`Backbone` in `detector.py`), so a comparison changes one thing only.

| `--detector` | What it is | Licence |
|---|---|---|
| `yolo` (default) | Ultralytics YOLO26, `models/yolo26m.pt` | AGPL-3.0 |
| `rfdetr` | RF-DETR Large, 704×704, `models/rf-detr-large-2026.pth` | Apache-2.0 |

RF-DETR is an optional dependency group, so it never enters the app's bundle
until it has won a comparison:

```bash
uv sync --group detectors
curl -L -o models/rf-detr-large-2026.pth https://storage.googleapis.com/rfdetr/rf-detr-large-2026.pth
```

(SHA-256 `0f4e20e19a99c0f8a62b5685f57f6c8b5c371c59081feda6752a0561a79ccf38`,
130 MB. The XL and 2XL variants need the `rfdetr[plus]` extension and are
licensed PML 1.0, so they are not used.)

Each detector writes to its own folder (`<camera>/rfdetr-derotated/`), so one
clip can hold several recorded sets, and `uv run bench.py --all-detectors`
scores them side by side at crossing level. Every recorded set names the
detector, its weights' SHA-256, the picture size and the threshold, and a
gold-set score refuses to run over clips whose counts used different detectors.

Measured on this Mac (MPS), per picture: YOLO 60 ms at 640 px and 10 ms on a
320 px crop; RF-DETR 67 ms at its own 704 px. So RF-DETR costs about the same
on whole pictures, and about six times more inside the de-rotated pipeline,
which asks for roughly twenty crops a frame.

**What the first comparison found** (16 Sep 2026; CN-123 11:30, two cameras, the
same two minutes, same tracker and rule):

| Detection set | People a frame | Tracks | Proposed | Time |
|---|---|---|---|---|
| YOLO11s, de-rotated | 9.8 / 10.2 | 146 / 161 | 0 | 2 min 24 s |
| RF-DETR Large, naive | 15.0 / 15.0 | 108 / 96 | 0 | 1 min 42 s |
| RF-DETR Large, de-rotated | 56.6 / 59.9 | 215 / 219 | 4 | 38 min 55 s |

RF-DETR in the de-rotated pipeline reports about 57 people a frame where 11 to
13 are in view: the same person found in several overlapping rotated crops and
not merged, because the duplicate rules (`merge_duplicates`) were tuned to
YOLO's boxes. As wired, that combination is unusable, and at 0.1× real time it
is too slow to use anyway. RF-DETR on whole pictures is the interesting one:
faster than YOLO de-rotated, and seeing more people.

**No winner is declared.** None of these crossings has been counted by hand, so
there is nothing to score recall against, and `bench.py --all-detectors` says so
rather than picking. Until a clip is fully hand-counted, "sees more people" is
not evidence of counting better.

Anything detected in the people-free median frame (clothing racks, mannequins)
is treated as static and ignored when later detections match it closely.

Tracks have no long-term identity. A track that dies and is followed within
`stitch_gap_max_s` by one nearby, moving the same way, is joined to it.

### Following people between frames

The tool counts with ByteTrack, associating on box overlap (IoU). Two other
trackers and one other matrix are wired in so they can be measured on the same
recorded detections rather than argued about:

| `--tracker` | what it adds |
|---|---|
| `byte` (default) | ByteTrack: overlap, then a second pass over the weak detections |
| `botsort` | BoT-SORT without re-identification (re-ID needs the pictures, which a replay does not have) |
| `ocsort` | OC-SORT: repairs a track's history after it comes back from being lost |

| `--assoc` | what it measures the first association on |
|---|---|
| `iou` (default) | plain overlap, which is the same 0 for every box that misses |
| `giou` | generalised overlap, which still ranks boxes that do not overlap at all (`--tracker byte` only: the others put their own terms in the matrix) |

**What the first comparison found** (19 Sep 2026; CN-159 11:15, one clip, two
cameras, 20 crossings counted by hand), as `found / false`:

| Tracking | YOLO26m de-rotated | RF-DETR whole picture |
|---|---|---|
| `byte` / `iou` (what the tool counts with) | 15 / 10 | 11 / 5 |
| `byte` / `giou` | 16 / 17 | 14 / 8 |
| `botsort` / `iou` | 15 / 10 | 13 / 3 |
| `ocsort` / `iou` | 16 / 10 | 13 / 7 |

**No winner is declared, and the default has not changed.** Twenty crossings
from one store is one clip's worth of luck: "one more found" is one person.
Generalised overlap does what its authors claim — tracks fall from 1863 to 1561
and joined breaks from 691 to 465, so people are being followed further — but
it offers more crossings that turn out to be nobody. Adopting any of these
needs the held-out clip scored as well, which needs its clean twin counted
automatically first.

Weights live in `models/` and are never downloaded at run time. One-time setup:

```bash
mkdir -p models
curl -L -o models/yolo26m.pt https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26m.pt
curl -L -o models/yolo11m.pt https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11m.pt
```

Ultralytics is forced offline and its usage analytics are switched off. Its
licence is AGPL-3.0, which is fine for internal use; distributing this tool
would need Ultralytics' commercial licence. RF-DETR is Apache-2.0, so if it
ever matches YOLO on the gold set, switching would remove that question
altogether — which is a better reason to compare them than accuracy alone.

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

## 5. Check, and 6. Report

Both are the wizard's own steps now (*The count wizard*, above); the standalone
`review.py` and `export.py` commands were removed on 16 September 2026.

The **check step** plays each item as a short clip, cropped to its camera, with
the line, zones and the person's path drawn on, and asks per camera:

- every crossing the tool counted: how many people crossed there (`Y` for one,
  `2`–`5` for a group, `N` for nobody, `U` when you cannot tell);
- every likely miss it listed — rejections of a kind that are often real, and
  tracks lost at the line;
- **a seeded quarter of the rule's other rejections**, so the counting rule is
  audited rather than trusted (a restored one is counted, and the report says it
  came from the audit);
- every stretch of movement it could not explain, at 2×, where you press `I` or
  `O` for anyone it never detected.

Every answer is saved as it happens, with who gave it and when, in
`runs/<video>/wizard/state.json` and the audit log.

The **report** is the PowerPoint described above. Finalising a validation also
writes `intervals.csv`, `crossings.csv`, the decisions and a manifest to
`validations/<ID>/`, read-only and checksummed.

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
  checks each picture itself rather than trusting an answer: a gold clip on marked
  footage is kept apart from clean ones, and any result counted or checked on it is left out of
  the comparison with the sensor, with the reason
  (docs/GROUND_TRUTH_SPECIFICATION.md, section 10). Earlier results can be
  audited for this on the *Validation runs* page.
- A counting system reports whole 15-minute intervals, and exports rarely match
  them to the second. Footage more than half a minute short of the intervals it
  is compared with is said so, on the report's first page and in the validation
  checks; the numbers are never scaled to fit. An interval the footage only
  brushes (a second or two, when an export runs a moment past the quarter hour)
  is left out of the comparison altogether.
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
