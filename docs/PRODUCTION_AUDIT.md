# Production audit: CrossingCount 0.1

Date: 14 September 2026. Scope: the whole repository at commit c09eba7 (about 20,000
lines of Python, HTML and build files; 224 automated tests). Written before the
production work starts; the items marked **Stage 1** are being fixed with it.

## 1. What it is today

A local, single-user tool that turns CCTV-style footage (RetailNext sensor exports) into
human-verified crossing counts and compares them with the sensor's own counts, in a
PowerPoint report. It runs on the user's computer (a server on 127.0.0.1 shown in its
own window) and never sends footage anywhere. RetailNext's cloud API is used only for
the sensor's numbers and to export footage, with keys in the system credential store.

## 2. Architecture

| Stage | Modules | Output |
|---|---|---|
| Footage intake | `video.py` (probe, timestamp audit, clock from the file name), `layout.py` (camera tiles), `overlay.py`, `trace_line.py` (finds RetailNext's drawn line) | video facts, tiles |
| Camera set-up | `config.py` (site drawings in `sites/`), `webapp.py` + `setup.html`, `geometry.py` | line, inside side, mask/filter zones per camera |
| Motion gating | `gating.py` (`gate.py`) | `activity.json`: stretches with movement near the line |
| Detection | `detector.py` (YOLO via Ultralytics, rotated crops for wide-angle pictures, static-object filter), `derotate.py`; recorded to `detections.pkl` and replayable | detections |
| Tracking | `tracks.py` (own tracker: jump cuts, walking-speed joining) | tracks |
| Crossing logic | `crossing.py` (side changes with a margin), `rule.py` (mask/filter zones, same-track returns, duplicates) | counted crossings, discards with reasons |
| Candidates | `candidates.py` (`detect.py`) | `candidates.json`, `discarded.json`, `unexplained.json` (incl. `broken_track_at_line`) |
| Human check | wizard check step (`wizard.py`: Y / N / Unsure / group size, a seeded sample of the rule's rejections, then watching unexplained movement) | verified crossings, decision log |
| Count by hand | wizard hand step (watched ranges from `manual.py`, per-camera counts, uncertain marks) | hand counts with coverage |
| Persistence | JSON under `runs/<video>/` (`wizard/state.json`, `history/`), `settings.json`, credential store (keyring) | |
| Sensor data | `retailnext.py` (locations, traffic per 15 minutes, busiest window, video export), wizard `use_retailnext` | sensor counts |
| Reporting | `report_pptx.py` (customer PowerPoint), `runs.py` (CSV files of a finalised validation) | report |
| Evaluation | `bench.py` (where verified crossings fall), `evaluate.py` + `gold.py` + `gold.html` (gold set, crossing-level scoring, experiment records) | scores |
| Training | `heads.py`, `label.py`, `train_heads.py` (parked: no better than the current detector); `examples.py` (learning examples to a shared folder) | |
| App shell | `wizard_app.py` (FastAPI), `window.py` (pywebview window), `app.py` (one executable), `updates.py` (git), `releases.py` + `builtin.py` (GitHub builds, self-update, built-in keys) | |
| Build and CI | `packaging/` (PyInstaller, Inno Setup, Mac `.dmg`), two GitHub workflows (tests, build, self-test, publish) | installers, releases |

## 3. Strengths

- Human verification is the source of truth; the tool proposes, a person decides.
- An incomplete check is called incomplete (no accuracy figure), and unsure answers are
  never counted.
- Crossing-level scoring with one-to-one matching, 95% intervals and minimum samples;
  a gold set with fixed store splits and blind second counts.
- Detections are recorded, so a change to tracking or the rule is re-scored in seconds.
- Everything local; keys in the credential store; no telemetry (Ultralytics forced
  offline).
- Timing from frame timestamps, with checks for dropped frames.
- A growing test suite (224 tests) on synthetic video; installers and self-tests built
  in CI.

## 4. Weaknesses and technical debt

- **Large, coupled modules.** `wizard.py` (1,650 lines) holds the state machine, the
  report's data, pictures, RetailNext merging and the hand count. `wizard.html` is one
  1,300-line file.
- **Untyped state.** State files are dicts marked `wizard/1` but never validated; old
  files are upgraded by filling in defaults.
- ~~**Duplicated code.**~~ Fixed (16 Sep 2026): the legacy command-line flows are gone —
  `count.py`, `review.py`, `export.py`, their pages and their apps were deleted after the
  one thing the wizard lacked (the seeded audit of the rule's rejections) was folded into
  its check step. One video player remains (`wizard.html`; the other pages draw on
  pictures, not video), one report writer (`report_pptx.py`), and one matching
  (`evaluate.match`), which `bench.py` and `gold.py` both use. The readers for counts kept
  in the older formats stay, so earlier work is still scored.
- **Sensor comparison tied to RetailNext.** The wizard assumes RetailNext's 15-minute
  intervals; there is no generic sensor format.
- **One number for the sensor.** "Sensor accuracy" is 100 - |sensor - verified| /
  verified x 100 (floored at 0, undefined at 0 verified) per direction per clip: no
  interval-level error, no bias across clips, no uncertainty.
- **Background work in threads.** Closing the app during a count loses it (it is marked
  interrupted and must be run again).
- **pickle** for recorded detections: safe only because the files are the tool's own.
- **Identity is typed text.** "Who" in the decision log is a name anyone can type.
- **Fixed port** (8780).

## 5. Unsafe assumptions

- The footage's clock comes from the export's file name (no reading of the burned-in
  clock). Frame-drop checks exist, but a wrong file name shifts every time.
- Overlapping cameras (e.g. CN-123 PB1 and R2) are counted per camera; the same person
  can be in both.
- On marked footage our line is the sensor's; on clean footage the drawn line may differ
  from the sensor's, so counts can legitimately differ.
- RetailNext's time zone and "complete" flags are trusted.
- **Independence.** The busiest-window list showed RetailNext's counts before a count by
  hand, the automatic flow asked for RetailNext's number before the check, and counts by
  hand were made on footage showing the sensor's own tracks. Each can sway a count
  towards the sensor. (**Stage 1**)
- **No written definition of a crossing** for people; the only precise rule was the
  tool's own. (**Stage 1**: Ground Truth Specification v1.0)

## 6. Production blockers

1. **The local server trusted any web page.** It checked neither the Host nor the Origin
   of requests, so a web page open in the user's browser could trigger actions (quit,
   update, send brands to GitHub), and through DNS rebinding read footage and reports.
   (**Stage 1**: `localweb.py`)
2. Independence of the ground truth (section 5). (**Stage 1**)
3. No formal ground-truth definition or dataset versioning. (**Stage 1**)
4. Sensor validation lacks interval-level errors and uncertainty. (Stage 2)
5. Results are not immutable: a report can be made again over an old one; there is no
   run ID or manifest. (Stage 3)
6. Unsigned apps: macOS and Windows warn on first open. Commercial distribution needs an
   Apple Developer ID and a Windows code-signing certificate.
7. Built-in keys: the company's RetailNext keys can be built into colleagues' apps; they
   must never ship to customers.
8. Learning examples copy frames of people to a shared folder: needs a privacy review and
   a retention rule.
9. No retention: runs keep pictures of people indefinitely. (Stage 5)
10. Ultralytics YOLO is AGPL-3.0 (code and weights). Open item, set aside by decision
    for now; see section 8.

## 7. Recommended refactors (only where they pay)

- Move the report's data out of `wizard.py`; keep `wizard.py` the state machine.
- A sensor-adapter module with one normalised interval-count format (Stage 2).
- Validate state files against a schema when loaded.
- Retire the legacy review and count pages once nobody uses them (keeping the command
  line for batch work), removing two of the three players.
- One scorer: fold `bench.py`'s "where was it found" into `gold.py`.

## 8. Dependencies and licences

From the installed packages' own metadata (direct dependencies and notable others):

| Package | Version | Licence | Note |
|---|---|---|---|
| ultralytics | 8.4.149 | AGPL-3.0 | Also the weights (yolo11s/m). Distributing a closed app needs Ultralytics' commercial licence. Open item. |
| ultralytics-thop | 2.1.6 | AGPL-3.0 | Pulled in by Ultralytics |
| torch | 2.14.0 | BSD-3 / Apache-2.0 (mixed, permissive) | |
| torchvision | 0.29.0 | BSD | |
| opencv-python | 5.0.0.93 | Apache-2.0 | Bundles FFmpeg (LGPL): check the build before distribution |
| av (PyAV) | 18.1.0 | BSD-3 | Bundles FFmpeg: check whether the wheel's FFmpeg is an LGPL or GPL build |
| numpy | 2.4.6 | BSD-3 (mixed, permissive) | |
| lap | 0.5.13 | BSD-2 | |
| fastapi / starlette / uvicorn | 0.141.1 / 1.6.0 / 0.52.4 | MIT / BSD-3 / BSD-3 | |
| pydantic | 2.13.5 | MIT | |
| python-pptx / lxml | 1.0.2 / 6.1.3 | MIT / BSD-3 | |
| keyring | 25.7.0 | MIT | |
| pywebview / pyobjc | 6.2.1 / 12.2.2 | BSD-3 / MIT | Windows: Edge WebView2 runtime (Microsoft, redistributable) |
| Pillow | 12.3.0 | MIT-CMU | |
| tzdata | 2026.4 | Apache-2.0 | |
| matplotlib, polars, PyYAML, requests, psutil | | PSF-style, MIT, MIT, Apache-2.0, BSD-3 | via Ultralytics |
| PyInstaller | 6.22.3 | GPL-2.0+ with bootloader exception | The exception allows bundling any program |
| Inno Setup | 6 | Inno Setup licence | Free for commercial use |

A full licence review (docs/LICENSING.md, every bundled package) is Stage 5.

## 9. Security and privacy

- Footage stays on the computer; the server listens on 127.0.0.1 only. The Host/Origin
  gap (blocker 1) is the one real exposure found.
- Keys: RetailNext keys and the GitHub token in the credential store (Keychain /
  Credential Manager), never in files or logs; built-in keys are extractable from the
  app (colleagues only).
- Pictures of people are written to `runs/` (thumbnails, busiest frame, detections) and,
  if set, to a shared examples folder; nothing deletes them.
- Logs: the app log holds tracebacks, not keys; RetailNext error messages never echo a
  key.
- No accounts: anyone who can use the computer can use the app.
- Recorded detections are pickles: never load one from elsewhere.
- No dependency vulnerability scanning in CI.

## 10. Testing gaps

- Tests use synthetic video only: no regression set of real, difficult clips (it cannot
  live on GitHub; it needs a local suite).
- The real detector is not exercised end to end in CI (weights are only in the build job).
- The window (pywebview), the self-update install and the Windows silent reinstall are
  untested automatically.
- No lint or type check in CI (they run locally).
- Sensor comparison metrics beyond one accuracy number do not exist yet.

## 11. Implementation order

1. **Stage 1: foundations.** This audit; Ground Truth Specification v1.0; independent
   counting (clean footage, sensor numbers hidden until counted, what counts as a
   person per validation); the dataset specification with frozen, checksummed versions;
   the local-server guard.
2. **Stage 2: validation core.** Count-level engine for sensors, metrics (bias, MAE,
   RMSE, WAPE) with uncertainty, sensor adapters (RetailNext, CSV), automatic traffic
   levels.
3. **Stage 3: trust.** Validation runs (IDs, finalised and locked), manifests, detector
   record, audit log.
4. **Stage 4: annotation.** Frame stepping, editing time and direction, uncertain marks,
   notes, adjudication.
5. **Stage 5: hardening.** Local regression suite, checksums, security and privacy
   documents with retention and clean-up, CI lint/type/scan, install guide, readiness
   checklist.
