# Security

CrossingCount handles CCTV of real people and keys to a retailer's analytics account. This
says how it is protected, and where the sharp edges are.

## 1. It runs on one computer, unless shared

The server listens on `127.0.0.1`, so nothing outside the computer can reach it. That
alone is not enough — any page open in a browser on the same computer could still send it
requests, and a website pointing its own name at 127.0.0.1 ("DNS rebinding") could read the
answers. So every request must be addressed to this computer by name (the `Host` header),
and anything that changes something must not come from another site (the `Origin` header,
which browsers set and pages cannot forge). Programs that are not browsers send no Origin
and are allowed through. See `localweb.py`; tests in `tests/test_localweb.py`.

> **Do not change the bind address to `0.0.0.0`.** The Host check refuses a *browser* on the
> network, but any other client can set that header itself, so the port becomes a way into
> the footage, the counts and the built-in keys. Sharing on the network (below) is the
> designed way to reach the app from another computer.

### Sharing on this network

Switched on by someone at the computer running CrossingCount (**Share on this network** in
the header, or `wizard.py --network`), and off until then. It stays on across restarts until
switched off. See `network.py`; tests in `tests/test_network.py`.

- **A second listener.** It answers on every network address of the computer, port 8781;
  the usual one stays on `127.0.0.1:8780`. Switching sharing off closes it.
- **An access code.** Eight characters (about 40 bits), new each time sharing starts. After
  10 wrong codes in 10 minutes, even the right one is refused for a while. The right code
  gives the browser a random token in an `HttpOnly`, `SameSite=Strict` cookie, which a page
  from another site cannot send. Anything that changes something must also come from the
  app's own pages (the `Origin` check).
- **What stays with the computer running it**, whoever asks over the network
  (`wizard_app.HOST_ONLY`): connecting, reconnecting and removing RetailNext keys, building
  them into the apps, updates and their token, settings, quitting, opening Finder or
  Explorer, reclassifying earlier results, freezing a gold set version, and the sharing
  switch itself. The test checks that every one of these is a real route.
- **Files.** People on the network pick footage from the footage folders and
  CrossingCount's download folder only, never a path elsewhere on the disk.
- **Each person's own validation.** Two people cannot open the same footage's validation at
  once. It can be taken over once left unused for 15 minutes, and never while counting.
  Counts run one at a time on the computer, whoever started them.
- **The audit log** records the network address of anyone who acts from another computer.

**What people with the code can do:** everything the app does apart from the list above.
That includes watching any footage in the footage folders, reading every finalised
validation and report, and **downloading footage from RetailNext with this computer's
brands**. The keys never leave the computer, but the footage they unlock does. Give the
code only to people who may see all of that.

**What it does not protect against:** the network itself. Traffic is plain HTTP, so on a
network someone else controls (café or hotel Wi-Fi, a guest network), the code, the cookie
and the footage can be read in transit. Share only on the company's own network. Sharing
with anyone outside the company also raises the AGPL question in
[LICENSING.md](LICENSING.md).

## 2. Keys and tokens

- RetailNext access keys and secret keys live in the operating system's credential store
  (Keychain on macOS, Credential Manager on Windows) through `keyring`. They are never
  written to a file, a report, a log or git; `settings.json` keeps only subscription names.
- Keys are typed into the app's own page and go straight to the credential store. They are
  never sent back to the page, and a refusal from RetailNext is reported without echoing
  any part of a key.
- The installed app and the project folder keep separate credential-store entries
  (`CrossingCount app RetailNext`, `CrossingCount RetailNext`), because macOS only lets a
  program replace entries it made itself; each reads the other's if allowed.
- **Builds can carry keys.** With the repository secrets `RETAILNEXT_BRANDS` and
  `UPDATE_TOKEN` set, they are baked into the app so colleagues need no keys of their own.
  Anyone holding such a build can extract them. They are for colleagues only and must never
  reach a customer. If a laptop is lost: change the keys in RetailNext, revoke the token on
  GitHub, and build again.

## 3. What leaves the computer

Only these, and nothing else:

| Goes out | To | Carrying |
|---|---|---|
| Data queries (locations, traffic per interval) | `<subscription>.api.retailnext.net` | The access key (Basic auth), store and time |
| Video export requests and downloads | RetailNext, then its time-limited storage link | The key for the request; **no key** on the download link |
| Update checks and downloads | GitHub releases of this private repository | The update token, when one is built in |

Redirects from the API are never followed, so a key cannot be carried to another host.
Footage, frames, counts and reports are never uploaded anywhere. Ultralytics is forced
offline (`YOLO_OFFLINE=1`) and its usage analytics are switched off. Model weights are never
downloaded at run time; they are fetched once, pinned by SHA-256 in CI.

## 4. Files and paths

- Camera drawings can only be loaded from the `sites/` folder (`webapp.Setup._safe`).
- A validation's folder is opened by its ID, which must match `CC-VAL-<year>-<computer>-<n>`
  exactly; anything else is refused (`/api/runs/reveal`).
- Videos are opened by a path the person chose in the app. From another computer (sharing
  on), only paths inside the footage folders and the download folder are accepted.
- A finalised validation's folder is made read-only, and every file in it is checksummed in
  its manifest; the runs page checks them each time it opens.
- The audit log (`audit/audit.jsonl`) is a SHA-256 hash chain: a changed, removed or
  inserted entry shows.

## 5. The installers

Mac and Windows builds are **unsigned** unless a code-signing certificate is configured, so
both systems warn about an unknown publisher. Every release names the installer's SHA-256 so
a download can be checked before it is run; see [INSTALL_WINDOWS.md](INSTALL_WINDOWS.md),
which also says what signing costs and how to turn it on (two repository secrets).

The app updates itself by downloading a release and running it. That download is not from a
browser, so there is no "keep this file" prompt — which also means the checksum in the
release notes is the way to verify a build by hand.

## 6. Logging

Logs hold progress, camera names, counts and file names. They never hold keys, tokens or
frame contents. On a Mac the app writes to
`~/Library/Application Support/CrossingCount/logs/app.log`, on Windows to
`%LOCALAPPDATA%\CrossingCount\logs\app.log`.

## 7. Known gaps

- Anyone who can use the computer can use the app: there are no user accounts, and none are
  planned while it is a single-user desktop tool.
- The credential store protects keys at rest, but a program running as the same user can ask
  for them.
- Unsigned builds (above) until a certificate is bought.
- Automatic deletion of old footage-derived files is **not built yet**; see
  [PRIVACY.md](PRIVACY.md), section 4.

## 8. Reporting a problem

Tell the copyright holder directly, with what you did and what happened. Do not open a
public issue: the repository is private and the data it touches is a retailer's.
