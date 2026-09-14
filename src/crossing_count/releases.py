"""Updating the installed app from GitHub's builds, and what those builds carry.

GitHub builds the Mac app and the Windows installer on every push to main and publishes
each as a release of the (private) repository: mac-b<build> with the .dmg, win-b<build>
with the installer. The app knows which build it is (build.json, written in by the build)
and, while its page is open, asks GitHub for a newer one every half hour. Updating is one
button: the Mac app downloads the new one, puts it in its own place and starts again; on
Windows the new installer runs and starts the app again.

A private repository's releases can only be read with a GitHub token that can read it:
built into the app when the repository has the secret UPDATE_TOKEN, or typed once on the
page and kept in this computer's credential store, never in a file.

From the project folder (where the GitHub command line is signed in), the page can also
put this computer's RetailNext brands, or such a token, into the repository's secrets, so
that the next builds carry them (see builtin.py for what that exposes).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import keyring
import keyring.errors

from . import builtin, paths
from .retailnext import Connection, _context, _no_redirect_open

SERVICE = "CrossingCount GitHub"
ACCOUNT = "token"
API = "https://api.github.com"
TIMEOUT_S = 30
KEEP_BUILDS = 3  # releases kept per platform (the workflows delete older ones)
WORKFLOWS = ("mac-app.yml", "windows-installer.yml")

_open = _no_redirect_open  # never follow a redirect with the token (tests replace these)
_download_open = urllib.request.urlopen  # GitHub's time-limited file link: it gets no token
_run = subprocess.run


class ReleaseError(Exception):
    """Something about updating, said plainly; nothing was changed."""


def platform() -> str:
    return "mac" if sys.platform == "darwin" else "win"


# ---- the token -----------------------------------------------------------------------------

def token() -> str | None:
    """The token typed on this computer, else the one built into the app."""
    with contextlib.suppress(keyring.errors.KeyringError):
        typed = keyring.get_password(SERVICE, ACCOUNT)
        if typed:
            return typed
    return builtin.github_token()


def _headers(tok: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}", "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "CrossingCount"}


def _get(path: str, tok: str) -> Any:
    req = urllib.request.Request(API + path, headers=_headers(tok))
    try:
        with _open(req, TIMEOUT_S, _context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ReleaseError("GitHub did not accept the token: it is wrong, expired or "
                               "revoked.") from None
        if e.code in (403, 404):
            raise ReleaseError("The token cannot read CrossingCount's repository: it needs "
                               "read access to its Contents.") from None
        raise ReleaseError(f"GitHub answered {e.code}.") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ReleaseError(f"GitHub could not be reached ({getattr(e, 'reason', e)}).") from None


def connect(tok: str) -> None:
    """A token typed on the page: tried on the repository first, then kept."""
    tok = tok.strip()
    info = builtin.build()
    if info is None:
        raise ReleaseError("This copy was not built by GitHub, so it has no builds to follow.")
    if not tok:
        raise ReleaseError("Enter the token.")
    _get(f"/repos/{info['repo']}/releases?per_page=1", tok)
    keyring.set_password(SERVICE, ACCOUNT, tok)


def forget_token() -> None:
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(SERVICE, ACCOUNT)


# ---- is there a newer build? ---------------------------------------------------------------

def newest(releases: list[dict[str, Any]], plat: str, build: int) -> dict[str, Any] | None:
    """The newest release of this platform that is newer than build and has its file."""
    best: tuple[int, dict[str, Any]] | None = None
    for r in releases:
        m = re.fullmatch(rf"{plat}-b(\d+)", str(r.get("tag_name", "")))
        if not m or r.get("draft") or not r.get("assets"):
            continue
        n = int(m[1])
        if n > build and (best is None or n > best[0]):
            best = (n, r)
    return None if best is None else {"build": best[0], "release": best[1]}


def _changes(repo: str, tok: str, mine: str, theirs: str, fallback: str) -> list[str]:
    """What changed between this build and the newer one, newest first."""
    try:
        data = _get(f"/repos/{repo}/compare/{mine}...{theirs}", tok)
        lines = [str(c["commit"]["message"]).splitlines()[0] for c in data.get("commits", [])]
    except (ReleaseError, KeyError, TypeError, IndexError):
        return [fallback] if fallback else []
    return list(reversed(lines))[:10]


def status() -> dict[str, Any]:
    """Like updates.status, for the installed app: a newer build on GitHub?"""
    out: dict[str, Any] = {"supported": False, "reason": "", "installed": True, "behind": 0,
                           "available": False, "changes": [], "restart": False, "notes": [],
                           "needs_token": False, "platform": platform()}
    info = builtin.build()
    if info is None:
        out["reason"] = ("This copy was not built by GitHub: it is updated by installing a new "
                         "one.")
        return out
    out.update(build=int(info["build"]), platform=info["platform"])
    tok = token()
    if not tok:
        out.update(needs_token=True, reason="Enter a GitHub token once to hear of new versions.")
        return out
    out["supported"] = True
    try:
        found = newest(_get(f"/repos/{info['repo']}/releases?per_page=50", tok),
                       str(info["platform"]), int(info["build"]))
    except ReleaseError as e:
        out["notes"].append(str(e))
        return out
    if found:
        rel = found["release"]
        out.update(available=True, behind=found["build"] - int(info["build"]),
                   latest=found["build"], size=int(rel["assets"][0].get("size") or 0),
                   changes=_changes(str(info["repo"]), tok, str(info["commit"]),
                                    str(rel.get("target_commitish") or rel["tag_name"]),
                                    str(rel.get("body") or rel.get("name") or "")))
    return out


def local_status() -> dict[str, Any]:
    """Which build is running, without asking GitHub (while waiting for a restart)."""
    info = builtin.build() or {}
    return {"supported": bool(info), "installed": True, "build": info.get("build"),
            "available": False, "behind": 0, "changes": [], "restart": False, "notes": []}


# ---- getting it ----------------------------------------------------------------------------

def download(progress: Callable[[int, int | None], None] | None = None) -> Path:
    """The newest build's file, into the data folder's updates/."""
    info, tok = builtin.build(), token()
    if info is None or not tok:
        raise ReleaseError("This copy cannot see GitHub's builds.")
    found = newest(_get(f"/repos/{info['repo']}/releases?per_page=50", tok),
                   str(info["platform"]), int(info["build"]))
    if not found:
        raise ReleaseError("There is no newer version to install.")
    asset = found["release"]["assets"][0]
    folder = paths.data_root() / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():  # one update at a time; an earlier one is no use now
        if old.is_file():
            old.unlink(missing_ok=True)
    dest = folder / Path(str(asset["name"])).name
    req = urllib.request.Request(f"{API}/repos/{info['repo']}/releases/assets/{asset['id']}",
                                 headers=_headers(tok, "application/octet-stream"))
    try:
        with _open(req, TIMEOUT_S, _context()) as resp:  # some answers carry the file itself
            _save(resp, dest, progress)
    except urllib.error.HTTPError as e:
        where = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
        if not where or not where.startswith("https://"):
            raise ReleaseError(f"GitHub did not hand over the file ({e.code}).") from None
        try:
            with _download_open(urllib.request.Request(where), timeout=120,
                                context=_context()) as resp:
                _save(resp, dest, progress)
        except (urllib.error.URLError, TimeoutError) as err:
            raise ReleaseError(f"The download stopped ({getattr(err, 'reason', err)}).") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ReleaseError(f"The download stopped ({getattr(e, 'reason', e)}).") from None
    size = int(asset.get("size") or 0)
    if size and dest.stat().st_size != size:
        dest.unlink(missing_ok=True)
        raise ReleaseError("The download was incomplete; nothing was changed. Try again.")
    return dest


def _save(resp: Any, dest: Path, progress: Callable[[int, int | None], None] | None) -> None:
    part = dest.with_name(dest.name + ".part")
    total = int(resp.headers.get("Content-Length") or 0) or None
    done = 0
    try:
        with open(part, "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)


# ---- putting it in place -------------------------------------------------------------------

def running_app() -> Path | None:
    """The CrossingCount.app this process runs from (Contents/MacOS/CrossingCount)."""
    exe = Path(sys.executable).resolve()
    app = exe.parents[2] if len(exe.parents) > 2 else None
    return app if app is not None and app.suffix == ".app" else None


def install_mac(dmg: Path, app: Path) -> None:
    """Swap app for the one in dmg. The old one is renamed aside first and removed only
    once the new one is in place, so a failure leaves the old app working."""
    mount = Path(tempfile.mkdtemp(prefix="crossingcount-update-"))
    r = _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint",
              str(mount), str(dmg)], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise ReleaseError(f"The downloaded disk image would not open: {r.stderr.strip()[:200]}")
    new = app.with_name(f".{app.stem}-new.app")
    old = app.with_name(f".{app.stem}-old.app")
    try:
        src = mount / app.name
        if not src.is_dir():
            src = next(mount.glob("*.app"), src)
        if not src.is_dir():
            raise ReleaseError("The disk image holds no app.")
        for p in (new, old):
            shutil.rmtree(p, ignore_errors=True)
        r = _run(["ditto", str(src), str(new)], capture_output=True, text=True, check=False)
        if r.returncode != 0:
            shutil.rmtree(new, ignore_errors=True)
            raise ReleaseError(f"The new app could not be copied: {r.stderr.strip()[:200]}")
        app.rename(old)
        try:
            new.rename(app)
        except OSError:
            old.rename(app)  # put the old one back
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        _run(["hdiutil", "detach", str(mount), "-quiet"], capture_output=True, check=False)
        shutil.rmtree(mount, ignore_errors=True)


def installer_args(installer: Path, app_dir: Path, local_appdata: str | None) -> list[str]:
    """A silent install into the same place: per user when it was installed per user."""
    per_user = bool(local_appdata) and str(app_dir).lower().startswith(str(local_appdata).lower())
    return [str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
            "/CURRENTUSER" if per_user else "/ALLUSERS"]


def install(file: Path) -> Callable[[], None] | None:
    """Put the downloaded build in place. Returns how to start the new version (then this
    process must end), or None when the person has to finish by hand (the Mac app sits
    where it cannot replace itself: the disk image is opened for them)."""
    if platform() == "mac":
        app = running_app()
        if app is None:
            raise ReleaseError("This is not running from CrossingCount.app.")
        if not os.access(app.parent, os.W_OK):
            _run(["/usr/bin/open", str(file)], check=False)
            return None
        install_mac(file, app)

        def start_mac() -> None:  # once this one has closed, the new app opens its window
            subprocess.Popen(["/bin/sh", "-c", 'sleep 2; exec /usr/bin/open -n "$0"', str(app)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        return start_mac

    args = installer_args(file, Path(sys.executable).resolve().parent,
                          os.environ.get("LOCALAPPDATA"))

    def start_windows() -> None:  # the installer starts the app again when it is done
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen(args, creationflags=flags, close_fds=True)
    return start_windows


def run_update(job: dict[str, Any], stop: Callable[[], None]) -> None:
    """Download, install and restart, reporting in job (read by the page)."""
    try:
        job.update(state="downloading", message="Downloading the new version…")
        file = download(lambda done, total: job.update(done=done, total=total))
        job.update(state="installing", message="Installing the new version…")
        start = install(file)
        if start is None:
            job.update(state="manual", message=(
                "CrossingCount could not replace itself where it is. The new version is open "
                "in Finder: drag CrossingCount to Applications, replacing the old one, then "
                "quit this one and open the new one."))
            return
        job.update(state="restarting", message="Starting the new version…")
        start()
        stop()
    except (ReleaseError, OSError) as e:
        job.update(state="failed", message=f"The update did not happen: {e}")


# ---- building brands and a token into the apps (from the project folder) ------------------

def _gh() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh"):
        if Path(p).is_file():
            return p
    return None


def can_share() -> bool:
    """The project folder, with the GitHub command line installed (and signed in)."""
    return not paths.FROZEN and (paths.SOURCE_ROOT / ".git").exists() and _gh() is not None


def _gh_run(*args: str, secret: str | None = None) -> subprocess.CompletedProcess[str]:
    gh = _gh()
    if gh is None or paths.FROZEN:
        raise ReleaseError("This needs the project folder and the GitHub command line.")
    return _run([gh, *args], cwd=str(paths.SOURCE_ROOT), input=secret, capture_output=True,
                text=True, timeout=120, check=False)


def _set_secret(name: str, value: str) -> None:
    r = _gh_run("secret", "set", name, secret=value)  # read from stdin: never on a command line
    if r.returncode != 0:
        raise ReleaseError(f"GitHub did not take it: {(r.stderr or r.stdout).strip()[:300]}")


def rebuild() -> list[str]:
    """Start new builds of both apps now, rather than at the next push."""
    notes = []
    for wf in WORKFLOWS:
        r = _gh_run("workflow", "run", wf, "--ref", "main")
        if r.returncode != 0:
            notes.append(f"{wf} did not start: {(r.stderr or r.stdout).strip()[:200]}")
    return notes


def share_brands(conns: list[Connection]) -> dict[str, Any]:
    """This computer's RetailNext brands, into the secret the builds read."""
    if not conns:
        raise ReleaseError("No brand is connected on this computer.")
    _set_secret("RETAILNEXT_BRANDS", json.dumps(
        {c.subscription: {"access_key": c.access_key, "secret_key": c.secret_key} for c in conns}))
    return {"brands": [c.subscription for c in conns], "notes": rebuild()}


def share_update_token(tok: str) -> dict[str, Any]:
    """A token that can read the repository, tried first, into the secret the builds read."""
    tok = tok.strip()
    if not tok:
        raise ReleaseError("Enter the token.")
    r = _gh_run("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    repo = r.stdout.strip()
    if r.returncode != 0 or not repo:
        raise ReleaseError("The GitHub command line cannot see this repository: sign in with it.")
    _get(f"/repos/{repo}/releases?per_page=1", tok)
    _set_secret("UPDATE_TOKEN", tok)
    return {"notes": rebuild()}
