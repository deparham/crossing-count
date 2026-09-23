# Privacy

CCTV footage shows identifiable people. Everything here follows from that.

## 1. The footage leaves this computer only when someone sends it

Video is opened from the computer it is already on, or downloaded from the retailer's own
RetailNext account to it. It is never uploaded to a model service and never shared with the
makers of any library used here. The app's pages are served to this computer only (see
[SECURITY.md](SECURITY.md)), unless someone at it switches on **sharing on this network**.
Then people on the same network who have the access code see the footage, frames and reports
in their own browser, and can download reports to their own computer. The only things the
software sends by itself are data queries to RetailNext: store, camera and time.

There is one way footage goes further, and it is never automatic. On the report page, a
finished validation can be **delivered** to a folder for the customer, typically a
OneDrive or SharePoint library synced to this computer. It always copies the report and the
validation's record. It copies the footage **only when the person ticks the box for it on
that validation**, having first been shown the destination folder, every file and its size,
and having confirmed. The box is off every time; it is never remembered.

Before ticking it, know what it means:

- The footage belongs to the retailer, not to whoever runs this (section 6). Putting it in
  a shared library is a second place it lives, under someone else's control and backup, and
  usually a second country.
- Everyone with access to that library can see it, including people with no part in the
  validation.
- Deleting it from the folder is not enough on its own: cloud libraries keep version
  history and a recycle bin, and a synced copy may sit on other people's computers too.
- An hour of one camera is roughly 100 MB.

Sending the report alone carries frames of people as well (section 2), but a handful of
snapshots rather than every minute of everyone who walked past.

## 2. What is kept, and where

The data folder is the project folder when run from source, or
`~/Library/Application Support/CrossingCount` (macOS) / `%LOCALAPPDATA%\CrossingCount`
(Windows) for the installed app.

| What | Where | Holds pictures of people? |
|---|---|---|
| The footage you downloaded | wherever you saved it (usually Downloads) | **Yes** |
| A validation's working files | `runs/<video>/` | **Yes**: frames of the busiest moment, snapshots of each crossing |
| Recorded detections | `runs/<video>/<camera>/detections.pkl` | No pictures; boxes and times only |
| The count itself | `runs/<video>/wizard/state.json` | No pictures; times, directions, notes, who decided what |
| A finalised validation | `validations/<ID>/` | **Yes**, inside the report |
| The report | the PowerPoint file | **Yes**: a frame of the busiest moment and a snapshot per crossing |
| Gold clips | `gold/gold_v1/*.json` | No pictures; crossing times and directions |
| A delivered validation | `<shared>/<Brand>/<Brand Store - CODE>/` | **Yes**, inside the report; and the footage too when it was ticked |
| Learning examples | the shared folder set on the report page | **Yes**: frames around each crossing |
| Head-marking labels | `labels/` | **Yes**: frames |
| The audit log | `audit/audit.jsonl` | No pictures; actions, times, and the operator's name |

Names of people are not collected. The **operator's own name** is recorded with each count,
answer and correction, because a validation has to say who made it. The computer's login
name is recorded in the audit log for the same reason.

## 3. The shared team folder

If a shared folder is set (a SharePoint library synced by OneDrive, or a network share),
every gold clip, each validation's record and the learning examples are copied there, and
the learning examples include **frames showing people**. That folder must be one only the
team can open. Do not put it anywhere customers, contractors or the wider company can
browse.

## 4. Keeping and deleting

- **There is no automatic deletion yet.** Old runs, frames and examples stay until someone
  removes them. This is the largest privacy gap in the tool as it stands, and it is listed
  in [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md).
- To delete a validation's working files by hand, delete its `runs/<video>/` folder. The
  footage itself is wherever you saved it; delete that too.
- A finalised validation's folder is read-only on purpose. Deleting it deliberately is the
  only way it goes, and the audit log will still show that the validation existed.
- Reports you have sent to a customer are in their hands; the tool cannot recall them.

Agree a retention period with whoever owns the footage, and keep to it by hand until the
tool can do it.

## 5. Whose data it is, and whose duty

The footage belongs to the retailer whose cameras recorded it. They are responsible for
having a lawful basis for the recording, for signage, and for how long it may be kept; this
tool is used on their behalf and holds copies for as long as a validation needs them.
Nothing here counts as advice on what that law requires where you operate.

## 6. What the tool never does

- It never sends footage, frames or counts anywhere by itself. The one way they go further
  is a delivery someone asks for and confirms (section 1), to a folder on their own shared
  drive - never to us, and never to a service.
- It never tries to recognise faces or identify anyone. People are found as boxes; nothing
  connects a box to a person's identity.
- It never trains a model by itself. Learning examples are collected only when a shared
  folder is set, and any training is started by a person.
- It never puts anything about a person in a URL or a log line.
