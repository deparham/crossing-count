# Production readiness

Where CrossingCount stands as a thing a customer pays for and a vendor may argue with.
Status is one of **Done**, **Partly**, **Not started**, or **Blocked on data**. Evidence is
a test name, a file or a measurement — never an assertion. Written 16 September 2026,
updated 17 September 2026.

Blocked on data means the code is there and the number cannot exist yet: it needs clips
counted by hand, which costs annotation hours, not programming.

## 1. What a crossing is

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| A written definition, versioned, recorded with every count | **Done** | `docs/GROUND_TRUTH_SPECIFICATION.md` v1.2; `specification` in each result and gold clip; `tests/test_gold.py`, `tests/test_runs.py` | — |
| Rules for children and staff, set per validation to match the sensor | **Done** | Spec section 4; `Wizard.set_rules`; `tests/test_gold.py::test_what_counts_as_a_person_is_set_per_validation` | — |
| Uncertain crossings marked, never counted, never scored | **Done** | Spec section 3; `tests/test_annotation.py` | — |
| Corrections keep what was there before, with a reason | **Done** | `Wizard.manual_edit`; decision log and audit log; `tests/test_annotation.py` | — |

## 2. Independence from the sensor

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| Counts made on clean footage, checked in the picture itself | **Done** | Spec section 10.1; `independence.py`; `tests/test_independence.py::test_what_the_picture_shows_beats_what_anyone_says` | — |
| Automatic checks judged the same way as hand counts | **Done** | `Wizard.footage_marked`; `tests/test_independence.py::test_a_check_on_marked_footage_is_not_independent_either` | — |
| Earlier results audited and reclassified | **Done** (code) | `independence.audit` / `reclassify`; runs page; `tests/test_independence.py::test_earlier_results_are_found_and_kept_out_of_the_comparison` | **Run it on this computer's own runs**: the Perri Cutten 392 check was made on marked footage and still counts as clean until the audit is run from the Validation runs page |
| Gold clips refused unless clean; unproven ones provisional | **Done** | `gold.problems`, `gold.provisional`; `tests/test_independence.py::test_provisional_clips_are_kept_listed_and_left_out_of_scoring` | — |
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
| Crossing-level recall of the automatic counter | **Blocked on data** | `evaluate.py`, `bench.py`, `gold.py` all in place and tested | No clip has been counted by hand in full, so recall is unmeasured |
| Scenario metrics (crowds, groups, lighting) | **Blocked on data** | Tags and conditions recorded per clip (`gold.py`) | Needs clips in each condition |
| How far two people agree | **Blocked on data** | `evaluate.agreement`, adjudication; `tests/test_annotation.py` | Needs a clip counted by a second person |

## 4. The record

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| Finalised validations, read-only, checksummed | **Done** | `runs.py`; `tests/test_runs.py::test_a_finalised_validation_is_kept_and_locked` | — |
| Corrections as new versions, never edits | **Done** | `Wizard.new_version`; `tests/test_runs.py::test_a_correction_is_a_new_version` | — |
| Tamper-evident audit log | **Done** | `auditlog.py` hash chain; `tests/test_runs.py::test_the_audit_log_shows_any_edit` | — |
| Manifests say what was compared with what | **Done** | `runs.write`: software, hardware, footage checksum, lines, correspondence, sampling, detector and weights | — |
| Which detector produced every number | **Done** | `detector.py` backbones; `candidates.json` → `detector`; `gold.evaluate` refuses mixed detectors; `tests/test_detectors.py` | — |
| Typed, validated state files with migrations | **Partly** | `wizard/schema.py`: a wizard state file is checked on opening (JSON, required fields, fingerprint, schema version) and an older one migrated without losing a field; the message names the file and the problem; `tests/test_schema.py` | Gold clips and manifests are still checked field by field where they are read, not against a schema |

## 5. The service

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| What checking a clip costs, per camera-hour | **Done** | `bench.totals`: questions, review minutes (measured from answer times), share left to watch; `tests/test_detectors.py::test_the_benchmark_says_what_the_checking_costs` | Needs more clips before a rate can be quoted |
| Things that never move counted separately | **Done** | `bench.static_objects`; `tests/test_detectors.py` | — |
| Detector comparison | **Done, inconclusive** | README "What the first comparison found": RF-DETR de-rotated reports ~57 people a frame where 11–13 are in view and runs at 0.1× real time; RF-DETR naive is faster than YOLO de-rotated | No winner without hand-counted clips; RF-DETR's duplicate merging needs its own thresholds before it is comparable in the de-rotated pipeline |
| One way to count, one way to check, one way to score | **Done** | Legacy `count.py`, `review.py`, `export.py` and their pages deleted; `bench.py` and `gold.py` share `evaluate.match` | — |
| A report a non-technical buyer can read | **Done** | PowerPoint and PDF from the same data (`report_pptx.py`, `report_pdf.py`); page 1 opens with the result in words, then the sample it rests on, the caveats (store totals, marked footage, lines drawn by eye), and the validation ID, sampling mode, specification, gold set and footage; a finalised report carries its ID; `tests/test_report_pdf.py`, `tests/test_runs.py::test_a_finalised_validation_is_kept_and_locked` | Not yet read by a buyer: the wording is untested on the people it is for |

## 6. Running it

| Item | Status | Evidence | Remaining |
|---|---|---|---|
| Lint, types and tests on every change | **Done** | `.github/workflows/checks.yml`; 273 tests | — |
| Dependencies checked for known vulnerabilities | **Done** | `pip-audit` job; none found on 16 Sep 2026 across 307 packages | — |
| One formatting standard, enforced | **Not started** | `ruff format --check` would rewrite 87 of 108 files | A one-off reformat commit, then add the check |
| Signed installers | **Partly** | Signing wired into the Windows build; publisher metadata; SHA-256 in release notes; `docs/INSTALL_WINDOWS.md` | Needs a certificate (about US$10 a month for Azure Trusted Signing); until then both systems warn |
| Local-only by construction | **Done** | `localweb.py` Host/Origin guard; `tests/test_localweb.py` | An uncommitted change to `wizard.py` binds to `0.0.0.0`; it must not be committed (see `docs/SECURITY.md`) |
| Licensing understood and written down | **Done** | `LICENSE`, `NOTICE`, `docs/LICENSING.md` with every dependency | A lawyer should confirm the AGPL position before any build leaves the company |
| Security and privacy written down | **Done** | `docs/SECURITY.md`, `docs/PRIVACY.md` | — |
| Deleting footage-derived data on a schedule | **Not started** | Everything is kept until someone deletes it (`docs/PRIVACY.md` section 4) | The largest privacy gap: automatic deletion of old runs, frames and examples, keeping the verified record |

## 7. The short version

Ready: the method (what a crossing is, independence, sampling scope, what may be claimed),
the record (finalised runs, audit log, manifests, detector provenance), and the paperwork
(licensing, security, privacy).

Not ready: **nothing has been counted by hand in full**, so the automatic counter's recall
is unknown and no percentage has an uncertainty range behind it. That is the one thing no
amount of code fixes, and everything in section 3 marked *blocked on data* waits on it.

Also outstanding: schemas for gold clips and manifests (section 4), automatic deletion of
old footage-derived data (section 6), and a code-signing certificate.
