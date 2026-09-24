"""SharePoint itself, rather than the folder OneDrive keeps in step with it.

Writing into the synced folder and leaving the rest to OneDrive is what put a validation's
row on one computer and nowhere else: the workbook was rebuilt, the sync engine lost the file
it had been watching, and the change sat on the disk while the team filled the same rows in
by hand. Here the live workbook is fetched from the server, changed, and put back, so there
is no second copy to fall behind and no sync to wait on.

A put carries the eTag the file was fetched with, so a colleague who saved in between is
found out (412) instead of quietly written over.

Signing in is the device code flow: the person is given a short code to type into
microsoft.com/devicelogin on any device, and this polls until they have. Only the refresh
token comes back here, and it goes to the operating system's credential store (Keychain on
this Mac, Credential Manager on Windows) through keyring - never into a file, a report, a log
or git. The client id and tenant are not secrets and live in the settings beside the folders.

The tenant needs an app registration for this to sign in to, made once by whoever administers
it: a public client (no secret), "Allow public client flows" turned on, and delegated
Microsoft Graph permissions Files.ReadWrite.All and Sites.Read.All. Its Application
(client) ID and Directory (tenant) ID are what go in the settings.
"""

from __future__ import annotations

import contextlib
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import keyring
import keyring.errors

from . import paths

SERVICE = "CrossingCount Microsoft"  # the credential store's entries (the project folder's)
APP_SERVICE = "CrossingCount app Microsoft"  # the installed app's own entries
ACCOUNT = "graph"
LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
# offline_access is what allows a refresh token, so signing in is a once-only thing
# Sites is asked for read only: every /sites/ call here is a GET, for the id of a site and of
# a library. Everything written goes through /drives/{id}/..., which Files.ReadWrite.All
# covers. It is the difference between asking an administrator to allow "edit or delete items
# in all site collections" and "read items in all site collections".
SCOPES = "offline_access Files.ReadWrite.All Sites.Read.All User.Read"
CHUNK = 8 * 1024 * 1024  # what goes up in one piece of a large upload; a multiple of 320 KiB
SMALL = 4 * 1024 * 1024  # up to this, a file goes up in a single put
TIMEOUT = 60.0
EARLY = 120.0  # refresh an access token this long before it runs out


class GraphError(Exception):
    """Something SharePoint would not do, said plainly."""


class Clash(GraphError):
    """Somebody else saved the file between reading it and writing it back."""


class NotSignedIn(GraphError):
    """No Microsoft account is connected yet."""


def _services() -> tuple[str, str]:
    return (APP_SERVICE, SERVICE) if paths.FROZEN else (SERVICE, APP_SERVICE)


def registration() -> tuple[str, str]:
    """The tenant and client id to sign in against, from the settings."""
    kept = paths.load_settings()
    tenant = str(kept.get("graph_tenant") or "").strip()
    client = str(kept.get("graph_client_id") or "").strip()
    if not tenant or not client:
        raise NotSignedIn(
            "Microsoft sign-in is not set up yet. Whoever administers your Microsoft 365 "
            "tenant registers an application for this once - a public client with the "
            "delegated Graph permissions Files.ReadWrite.All and Sites.Read.All - and "
            "its Application (client) ID and Directory (tenant) ID go on the Report page.")
    return tenant, client


def _post(url: str, form: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            out: dict[str, Any] = json.loads(r.read() or b"{}")
            return out
    except urllib.error.HTTPError as exc:
        said = {}
        with contextlib.suppress(Exception):
            said = json.loads(exc.read() or b"{}")
        if said.get("error"):  # the flow's own words: still waiting, expired, declined
            return dict(said)
        raise GraphError(f"Microsoft would not answer ({exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GraphError(f"Microsoft could not be reached ({exc}).") from exc


@dataclass
class SignIn:
    """A sign-in waiting for the person to type its code in."""

    code: str  # what they type
    url: str  # where they type it
    message: str  # Microsoft's own wording of both
    device_code: str  # what this polls with; not shown
    interval: float
    expires_at: float


def begin() -> SignIn:
    """Start a sign-in: gives back the code for the person to enter."""
    tenant, client = registration()
    said = _post(f"{LOGIN}/{tenant}/oauth2/v2.0/devicecode",
                 {"client_id": client, "scope": SCOPES})
    if "device_code" not in said:
        raise GraphError(f"Microsoft would not start the sign-in "
                         f"({said.get('error_description') or said.get('error') or 'no reason'}).")
    return SignIn(code=str(said["user_code"]), url=str(said["verification_uri"]),
                  message=str(said.get("message") or ""), device_code=str(said["device_code"]),
                  interval=float(said.get("interval") or 5), expires_at=time.time()
                  + float(said.get("expires_in") or 900))


def poll(started: SignIn) -> bool:
    """Ask once whether they have typed the code in yet. True when they have."""
    tenant, client = registration()
    said = _post(f"{LOGIN}/{tenant}/oauth2/v2.0/token", {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client, "device_code": started.device_code})
    if said.get("refresh_token"):
        _remember(said)
        return True
    err = str(said.get("error") or "")
    if err in ("authorization_pending", "slow_down"):
        return False
    raise GraphError({
        "expired_token": "The sign-in code ran out before it was entered. Start again.",
        "authorization_declined": "The sign-in was declined.",
        "bad_verification_code": "That sign-in is no longer valid. Start again.",
    }.get(err, f"The sign-in did not finish ({said.get('error_description') or err})."))


def _remember(said: dict[str, Any]) -> None:
    """Keep the refresh token, and the access token while it lasts, in the credential store."""
    kept = {"refresh_token": said["refresh_token"], "access_token": said.get("access_token", ""),
            "expires_at": time.time() + float(said.get("expires_in") or 0)}
    try:
        keyring.set_password(_services()[0], ACCOUNT, json.dumps(kept))
    except keyring.errors.KeyringError as exc:
        raise GraphError(f"The sign-in could not be kept in the credential store ({exc}). "
                         f"Nothing was written anywhere else.") from exc


def _kept() -> dict[str, Any]:
    for service in _services():
        with contextlib.suppress(keyring.errors.KeyringError, ValueError):
            if raw := keyring.get_password(service, ACCOUNT):
                got: dict[str, Any] = json.loads(raw)
                return got
    return {}


def signed_in() -> bool:
    return bool(_kept().get("refresh_token"))


def forget() -> None:
    """Sign out: the tokens leave the credential store and nothing of them is kept."""
    for service in _services():
        with contextlib.suppress(keyring.errors.KeyringError):
            keyring.delete_password(service, ACCOUNT)


def token() -> str:
    """An access token to call Graph with, refreshed when it is about to run out."""
    kept = _kept()
    if not kept.get("refresh_token"):
        raise NotSignedIn("No Microsoft account is connected. Sign in on the Report page.")
    if kept.get("access_token") and float(kept.get("expires_at") or 0) - EARLY > time.time():
        return str(kept["access_token"])
    tenant, client = registration()
    said = _post(f"{LOGIN}/{tenant}/oauth2/v2.0/token", {
        "grant_type": "refresh_token", "client_id": client, "scope": SCOPES,
        "refresh_token": str(kept["refresh_token"])})
    if not said.get("access_token"):
        raise NotSignedIn(f"The Microsoft sign-in has run out "
                          f"({said.get('error_description') or said.get('error') or 'no reason'}). "
                          f"Sign in again on the Report page.")
    said.setdefault("refresh_token", kept["refresh_token"])  # not always sent again
    _remember(said)
    return str(said["access_token"])


def call(method: str, url: str, *, body: bytes | None = None,
         headers: dict[str, str] | None = None, raw: bool = False) -> Any:
    """One Graph call. Gives back what was read, parsed unless `raw` says otherwise."""
    if not url.startswith("http"):
        url = GRAPH + url
    head = {"Authorization": f"Bearer {token()}", **(headers or {})}
    req = urllib.request.Request(url, data=body, method=method, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            read = r.read()
            if raw:
                return read, dict(r.headers)
            return json.loads(read) if read else {}
    except urllib.error.HTTPError as exc:
        raise _said(exc, url) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GraphError(f"SharePoint could not be reached ({exc}).") from exc


def _said(exc: urllib.error.HTTPError, url: str) -> GraphError:
    detail = ""
    with contextlib.suppress(Exception):
        detail = str(json.loads(exc.read() or b"{}").get("error", {}).get("message") or "")
    if exc.code == 412:
        return Clash("Somebody else saved the file while this was being prepared. Nothing was "
                     "written; look at it again and it will be read afresh.")
    if exc.code == 423:
        return GraphError("The file is locked, usually because it is open in Excel. Close it "
                          "and try again.")
    if exc.code in (401, 403):
        return GraphError(f"Microsoft would not allow that ({detail or exc.code}). The "
                          f"sign-in may need renewing, or the account may not have access.")
    if exc.code == 404:
        return GraphError(f"Not found on SharePoint: {urllib.parse.unquote(url)}")
    return GraphError(f"SharePoint would not do that ({exc.code}{': ' + detail if detail else ''}).")


def _quote(path: str) -> str:
    return urllib.parse.quote(str(path).strip("/"), safe="/")


# Where a thing is on SharePoint, worked out from the synced folder's own name: a folder
# ".../CloudStorage/OneDrive-Acme/validations - Documents/External/RAG" is External/RAG of the
# Documents library of the site "validations". Only the host has to be learned from elsewhere.
SYNCED = re.compile(r"CloudStorage/OneDrive-[^/]+/(?P<site>[^/]+?) - (?P<library>[^/]+)/(?P<rest>.+)")
DEFAULT_LIBRARY = {"Documents": "Shared Documents"}  # what the default library is called there


@dataclass(frozen=True)
class Place:
    """Somewhere in a document library: which site, which library, and where in it."""

    site: str  # https://acme.sharepoint.com/sites/validations
    library: str  # the library's name, or "" for the site's default one
    path: str  # External/RAG/Connor/Connor Northland - CN-212

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def under(self, *more: str) -> Place:
        return Place(self.site, self.library, "/".join([self.path, *more]).strip("/"))


_drives: dict[tuple[str, str], str] = {}  # what Graph called each library, asked once


def drive_of(place: Place) -> str:
    if (place.site, place.library) not in _drives:
        _drives[(place.site, place.library)] = drive_id(site_id(place.site), place.library)
    return _drives[(place.site, place.library)]


def place_for(local: Path, host: str) -> Place | None:
    """Where a folder on the synced drive is on SharePoint, or None when it cannot be told."""
    m = SYNCED.search(local.as_posix())
    if not m or not host:
        return None
    return Place(site=f"https://{host}/sites/{m['site']}",
                 library=DEFAULT_LIBRARY.get(m["library"], m["library"]), path=m["rest"])


def site_id(url: str) -> str:
    """The site's id, from the address of a page or folder in it.

    "https://acme.sharepoint.com/sites/validations/..." asks Graph about acme.sharepoint.com's
    /sites/validations, which is the shape every link in the team's sheet has.
    """
    bits = urllib.parse.urlsplit(url if "://" in url else f"https://{url}")
    parts = [p for p in bits.path.split("/") if p]
    where = "/".join(parts[:2]) if parts[:1] in (["sites"], ["teams"]) else ""
    got = call("GET", f"/sites/{bits.netloc}:/{where}" if where else f"/sites/{bits.netloc}")
    return str(got["id"])


def drive_id(site: str, library: str = "") -> str:
    """The document library to work in: the site's default one unless another is named."""
    if not library:
        return str(call("GET", f"/sites/{site}/drive")["id"])
    for d in call("GET", f"/sites/{site}/drives").get("value", []):
        if str(d.get("name", "")).lower() == library.lower():
            return str(d["id"])
    raise GraphError(f"{site} has no document library called {library!r}.")


def item(drive: str, path: str) -> dict[str, Any]:
    """What SharePoint holds at this path, or {} when there is nothing there."""
    try:
        got: dict[str, Any] = call("GET", f"/drives/{drive}/root:/{_quote(path)}")
        return got
    except GraphError as exc:
        if "Not found" in str(exc):
            return {}
        raise


def read(drive: str, path: str) -> tuple[bytes, str]:
    """The file as it is on the server this moment, and the eTag it was read at."""
    got = item(drive, path)
    if not got:
        raise GraphError(f"There is no {path} on SharePoint.")
    blob, _ = call("GET", f"/drives/{drive}/items/{got['id']}/content", raw=True)
    return bytes(blob), str(got.get("eTag") or "")


def replace(drive: str, path: str, blob: bytes, etag: str = "") -> dict[str, Any]:
    """Put a file back, only if nobody has saved it since it was read."""
    head = {"Content-Type": "application/octet-stream"}
    if etag:
        head["If-Match"] = etag
    got: dict[str, Any] = call("PUT", f"/drives/{drive}/root:/{_quote(path)}:/content",
                               body=blob, headers=head)
    return got


def folder(drive: str, path: str) -> dict[str, Any]:
    """Make a folder, and every folder above it, unless it is there already."""
    here = ""
    made: dict[str, Any] = {}
    for part in [p for p in str(path).strip("/").split("/") if p]:
        parent, here = here, f"{here}/{part}".lstrip("/")
        if got := item(drive, here):
            made = got
            continue
        at = f"/drives/{drive}/root:/{_quote(parent)}:/children" if parent \
            else f"/drives/{drive}/root/children"
        made = call("POST", at, body=json.dumps({
            "name": part, "folder": {}, "@microsoft.graph.conflictBehavior": "fail",
        }).encode(), headers={"Content-Type": "application/json"})
    return made


def upload(drive: str, path: str, src: Path,
           on: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Put a file there, in pieces when it is big enough to need them.

    Never over one already there: SharePoint is asked to rename instead, because a delivered
    folder is the customer's and nothing of theirs is replaced (delivery.py says the same of
    the drive).
    """
    size = src.stat().st_size
    if size <= SMALL:
        got: dict[str, Any] = call(
            "PUT", f"/drives/{drive}/root:/{_quote(path)}:/content",
            body=src.read_bytes(), headers={"Content-Type": "application/octet-stream"})
        if on:
            on(size, size)
        return got
    session = call("POST", f"/drives/{drive}/root:/{_quote(path)}:/createUploadSession",
                   body=json.dumps({"item": {
                       "@microsoft.graph.conflictBehavior": "rename"}}).encode(),
                   headers={"Content-Type": "application/json"})
    url, sent = str(session["uploadUrl"]), 0
    with src.open("rb") as f:
        while piece := f.read(CHUNK):
            end = sent + len(piece) - 1
            got = call("PUT", url, body=piece, headers={
                "Content-Range": f"bytes {sent}-{end}/{size}",
                "Content-Length": str(len(piece))})
            sent = end + 1
            if on:
                on(sent, size)
    return got
