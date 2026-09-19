# Production readiness

Where CrossingCount stands as a thing a customer pays for and a vendor may argue with.
Status is one of **Done**, **Partly**, **Not started**, or **Blocked on data**. Evidence is
a test name, a file or a measurement — never an assertion. Written 16 September 2026,
last updated 19 September 2026.

Blocked on data means the code is there and the number cannot exist yet: it needs clips
counted by hand, which costs annotation hours, not programming.

## 1. What a crossing is

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| A written definition, versioned, recorded with every count | **Done** | `docs/GROUND_TRUTH_SPECIFICATION.md` v1.4; `specification` in each result and gold clip; `tests/test_gold.py`, `tests/test_runs.py` | — |
| A count that marks no crossings cannot claim to be one | **Done** | The total-only mode records no moments, so `result()["specification"]` is `None`: no version of what a crossing is was applied to it; `tests/test_total_count.py::test_a_saved_total_never_becomes_a_crossing` | — |
| Rules for children and staff, set per validation to match the sensor | **Done** | Spec section 4; `Wizard.set_rules`; `tests/test_gold.py::test_what_counts_as_a_person_is_set_per_validation` | — |
| Uncertain crossings marked, never counted, never scored | **Done** | Spec section 3; `tests/test_annotation.py` | — |
| Corrections keep what was there before, with a reason | **Done** | `Wizard.manual_edit`; decision log and audit log; `tests/test_annotation.py` | — |

## 2. Independence from the sensor

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| Counts made on clean footage, checked in the picture itself | **Done** | Spec section 10.1; `independence.py`; `tests/test_independence.py::test_what_the_picture_shows_beats_what_anyone_says` | — |
| Automatic checks judged the same way as hand counts | **Done** | `Wizard.footage_marked`; `tests/test_independence.py::test_a_check_on_marked_footage_is_not_independent_either` | — |
| Earlier results audited and reclassified | **Done** (code) | `independence.audit` / `reclassify`; runs page; `tests/test_independence.py::test_earlier_results_are_found_and_kept_out_of_the_comparison` | **Still to run on this computer's own runs.** The audit was run read-only on 19 Sep 2026 and finds exactly one result: the Perri Cutten 392 check, on footage downloaded with RetailNext's marks, whose name says so and whose counter said so. It counts as clean until someone reclassifies it from the Validation runs page |
| Gold clips on marked footage kept apart from clean ones; unproven clean ones provisional | **Done** | `gold.tier`, `gold.evaluate` (by tier; test set clean only; a window counted both ways once), `gold.provisional`; `tests/test_gold.py::test_marked_clips_are_scored_apart_and_never_in_the_test_set`, `tests/test_independence.py::test_provisional_clips_are_kept_listed_and_left_out_of_scoring` | — |
| How far RetailNext's marks sway a count | **Blocked on data** | `gold.marks_effect`: windows counted on clean and marked footage matched crossing by crossing, with the shift towards the system's number; `tests/test_gold.py::test_windows_counted_both_ways_show_how_far_the_marks_move_a_count` | Needs 5 windows counted both ways, ideally by different people |
| The line is where the sensor counts | **Partly** | `correspondence.py`; calibrate-on-marked flow; `tests/test_independence.py` (calibrated vs by eye, alignment on real exports: same camera 0.62, different cameras below 0.1) | RetailNext's API exposes no line geometry, so the strongest route is unavailable; drawn-by-eye lines remain the weakest evidence and are labelled as such |
| A camera is never silently the wrong camera | **Done** | `Setup._without_marks`; `Wizard.unconfirmed`; `tests/test_independence.py::test_a_camera_matched_by_its_name_alone_blocks_the_run` | — |

## 3. What the numbers may claim

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| Sampling scope stated, not hidden | **Done** | Spec section 9; `sampling.py`; scope sentence on the report's first page; `tests/test_sampling.py` | — |
| Control windows beside peak windows | **Done** | `sampling.plan` (peak + control); Sensor results names stores without one; `tests/test_sampling.py::test_peak_windows_come_with_a_control_window_of_lower_traffic` | Needs using: no control window has been validated yet |
| Error broken down by traffic level | **Done** | `validation.by_level`; report section and Sensor results card; `tests/test_sampling.py::test_the_error_is_broken_down_by_traffic_level` | — |
| No percentage on too few crossings | **Done** | `validation.quote` (30 verified minimum); `tests/test_engagement.py::test_eleven_against_nine_gives_the_counts_not_a_percentage` | — |
| A percentage with an uncertainty range | **Done** | Bootstrap over whole windows, `MIN_CLUSTERS` = 5; engagements; `tests/test_engagement.py::test_windows_put_together_give_a_percentage_and_its_range` | Needs 5 finalised windows of one store; none yet |
| Count-level metrics defined and tested | **Done** | `docs/METRICS_SPECIFICATION.md` section 2; `tests/test_validation.py` | — |
| Crossing-level recall of the automatic counter | **Partly** (first measurement) | CN-159, 15 minutes, two cameras, 20 crossings counted by hand (18 Sep 2026), scored 19 Sep: YOLO26m de-rotated found 15 of 20 (75%) with 10 false, RF-DETR on the whole picture 11 of 20 (55%) with 5 false | 20 crossings is below the 30 the tool requires before quoting a rate, and it is one store: more clips, in more stores, before any number is published |
| Why the tool misses crossings | **Done** (for the first clip) | Of 8 misses: 3 people seen but no crossing proposed, 2 tracks broken at the line, 3 rejected by the counting rule (two "returned on the same track", one "pending expired"). The mask rule tripped its own alarm: 33% of committed counts on CN-159-L1 against a 2% threshold | — |
| Whether the counting rule is the cause | **Done** (and it is not) | The rules were audited on CN-159, 19 Sep 2026: of 32 rejections, 26 were right and 6 were real crossings (returns 2 of 9, pending-expired 2 of 8, duplicate 1 of 7, outward-no-mask 1 of 5). Every pending-expired track had `ended_by=lost` within 0.2–1.4 s of the line, having moved 3 px or less, and the mask bands are 7–13 px thick and 25–30 px past it | The cause is tracks dying at the line, not the rules that read them: 1,042 tracks in 15 minutes on CN-159-PB2, 466 joined back together and 287 cut. See the tracker row in section 5 |
| A total counted by hand cannot be quoted as a crossing-level result | **Done** | A total-only count has no verified crossings, so no recall, precision or missed-crossing rate is worked out from it and the report says so outright; it compares with the system as one total against another, as a single row over the whole footage; `tests/test_total_count.py` | — |
| The counter's reaction time is not counted as the tool's error | **Done** | `evaluate.lag`: measured median 1.4 s over 30 hand-counted crossings (two stores, three cameras); clocks aligned before matching, window 3 s, both recorded with every score; `tests/test_evaluate.py::test_a_counters_reaction_time_is_measured_and_taken_out`; the hand-count page can move a mark onto the frame where the crossing happens | — |
| Scenario metrics (crowds, groups, lighting) | **Blocked on data** | Tags and conditions recorded per clip (`gold.py`) | Needs clips in each condition |
| How far two people agree | **Blocked on data** | `evaluate.agreement`, adjudication; `tests/test_annotation.py` | Needs a clip counted by a second person |
| Anything measured on held-out data | **Blocked on data** | The gold set holds two clips: CN-159 (clean, train split, scored) and CN-146 (marked, test split, counted by hand but with no automatic count of its clean twin, so `score_clip` refuses it) | **Nothing has been measured on the test split yet.** Counting CN-146's clean twin automatically is one command and makes every measurement so far checkable on data it was not chosen on |

## 4. The record

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| Finalised validations, read-only, checksummed | **Done** | `runs.py`; `tests/test_runs.py::test_a_finalised_validation_is_kept_and_locked` | — |
| Corrections as new versions, never edits | **Done** | `Wizard.new_version`; `tests/test_runs.py::test_a_correction_is_a_new_version` | — |
| Tamper-evident audit log | **Done** | `auditlog.py` hash chain; `tests/test_runs.py::test_the_audit_log_shows_any_edit` | — |
| Manifests say what was compared with what | **Done** | `runs.write`: software, hardware, footage checksum, lines, correspondence, sampling, detector and weights | — |
| Which detector produced every number | **Done** | `detector.py` backbones; `candidates.json` → `detector`; `gold.evaluate` refuses mixed detectors; `tests/test_detectors.py` | — |
| What has been validated, kept apart by how it was counted | **Done** | `runs.coverage`: counting crossing by crossing, counting a total, and checking the tool's crossings are counted separately, with whether each can be matched against the tool's one by one; Validation runs page; `tests/test_total_count.py::test_what_has_been_validated_is_counted_by_how_it_was_counted` | On this computer, 19 Sep 2026: 3 counted by hand (68 verified, 3 stores, 7 cameras) and 1 checked (21 verified, 2 cameras) |
| Typed, validated state files with migrations | **Partly** | `wizard/schema.py`: a wizard state file is checked on opening (JSON, required fields, fingerprint, schema version) and an older one migrated without losing a field; the message names the file and the problem; `tests/test_schema.py` | Gold clips and manifests are still checked field by field where they are read, not against a schema |

## 5. The service

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| What checking a clip costs, per camera-hour | **Done** | `bench.totals`: questions, review minutes (measured from answer times), share left to watch; `tests/test_detectors.py::test_the_benchmark_says_what_the_checking_costs` | Needs more clips before a rate can be quoted |
| Things that never move counted separately | **Done** | `bench.static_objects`; `tests/test_detectors.py` | — |
| Detector comparison | **Done, first evidence** | Scored against the same hand count (CN-159): YOLO26m de-rotated 75% recall / 60% precision, RF-DETR whole picture 55% / 69%. YOLO26m finds more real crossings and pays in false ones, which is the right way round for a count that is checked | One clip, one store, 20 crossings: a direction, not a verdict |
| A count for someone who only needs a number | **Done** | The total-only mode: a number per camera and direction, pressed up and down, with notes, needing no detector, tracker or drawn line. It is kept out of everything that matches crossings — no verified crossings, refused by the gold set, no truth to the benchmark, no specification version, nothing to run — and the presses are kept without the moment each was made, because a time per press would look like a crossing and is not one; `tests/test_total_count.py` (13 tests) | Not yet used by a colleague on the network, which is what it is for |
| Which tracker follows people, and on what | **Done** (choosable and measured) | `DetectOptions.tracker` / `.assoc`, `bench.Tracking`, `detect.py --tracker/--assoc`, `bench.py --gold`; ByteTrack, BoT-SORT (no re-ID) and OC-SORT replay the same recorded detections; a scoring made with anything but the default names it and says in its own limits that it is an experiment, not a result; `tests/test_fixes.py`, `tests/test_bench.py`, `tests/test_gold.py` | First comparison on CN-159 (found/false of 20, YOLO26m de-rotated): ByteTrack 15/10, BoT-SORT 15/10, **OC-SORT 16/10**, ByteTrack+GIoU 16/17. The default is unchanged: 20 crossings from one store is a direction, not a decision, and none of it is checked on the test split yet |
| One way to count, one way to check, one way to score | **Done** | Legacy `count.py`, `review.py`, `export.py` and their pages deleted; `bench.py` and `gold.py` share `evaluate.match` | — |
| A report a non-technical buyer can read | **Done** | PowerPoint and PDF from the same data (`report_pptx.py`, `report_pdf.py`); page 1 opens with the result in words, then the sample it rests on, the caveats (store totals, marked footage, lines drawn by eye), and the validation ID, sampling mode, specification, gold set and footage; a finalised report carries its ID; `tests/test_report_pdf.py`, `tests/test_runs.py::test_a_finalised_validation_is_kept_and_locked` | Not yet read by a buyer: the wording is untested on the people it is for |

## 6. Running it

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| Lint, types and tests on every change | **Done** | `.github/workflows/checks.yml`; 308 tests, mypy over 108 source files, ruff | — |
| Dependencies checked for known vulnerabilities | **Done** | `pip-audit` job; none found on 16 Sep 2026 across 307 packages | — |
| One formatting standard, enforced | **Not started** | `ruff format --check` would rewrite 87 of 108 files | A one-off reformat commit, then add the check |
| Signed installers | **Partly** | Signing wired into the Windows build; publisher metadata; SHA-256 in release notes; `docs/INSTALL_WINDOWS.md` | Needs a certificate (about US$10 a month for Azure Trusted Signing); until then both systems warn |
| Local by default; shared on the network only when switched on | **Done** | `localweb.py` Host/Origin guard; `network.py` (a second listener, an access code with lock-out, `HttpOnly`/`SameSite=Strict` cookie); `wizard_app.HOST_ONLY`; a validation per person; counts one at a time; `tests/test_localweb.py`, `tests/test_network.py`; tried by hand on a Mac from a second tab on its network address | Plain HTTP: share only on the company's own network. Whether colleagues using it over the network is within AGPL internal use needs a lawyer (`docs/LICENSING.md`) |
| Licensing understood and written down | **Done** | `LICENSE`, `NOTICE`, `docs/LICENSING.md` with every dependency | A lawyer should confirm the AGPL position before any build leaves the company |
| Security and privacy written down | **Done** | `docs/SECURITY.md`, `docs/PRIVACY.md` | — |
| Deleting footage-derived data on a schedule | **Not started** | Everything is kept until someone deletes it (`docs/PRIVACY.md` section 4) | The largest privacy gap: automatic deletion of old runs, frames and examples, keeping the verified record |

## 7. The short version

Ready: the method (what a crossing is, independence, sampling scope, what may be claimed),
the record (finalised runs, audit log, manifests, detector provenance), the three ways to
count and what each may be quoted as, and the paperwork (licensing, security, privacy).

Not ready, and this is the whole of it: **one clip has been counted by hand in full and
scored** (CN-159, 20 crossings). It is below the 30 the tool itself requires before quoting
a rate, it is one store, and it is in the train split — so every number measured so far, the
detector comparison and the tracker comparison included, was measured on the clip it was
chosen on. The second clip counted by hand (CN-146, the test split) has no automatic count
of its clean twin, so nothing has been checked on held-out data at all.

Everything in section 3 marked *blocked on data* waits on more clips, in more stores, and on
a second person counting one of them. That is annotation hours, not programming, and it is
the only thing standing between this and a number that can be published.

Also outstanding, in the order worth doing them:

1. **Deleting footage-derived data on a schedule** (section 6). Images of identifiable people
   are kept until someone deletes them by hand. This is the one outstanding item that is a
   legal exposure rather than a shortcoming.
2. **A certificate for signed installers** (section 6). Until then both systems warn the
   person installing it.
3. **A lawyer on the AGPL position** (section 6), before any build leaves the company.
4. **Schemas for gold clips and manifests** (section 4): the files the evidence chain rests on
   are the ones still checked field by field.
5. **A page telling a colleague how to do a validation.** The README is written for whoever
   builds this. If annotation hours are the constraint, the way out is other people, and
   nothing yet is written for them.
6. **One formatting standard** (section 6): a single reformat commit, then the check.
