"""Updating CrossingCount from its own page, when it runs from a git checkout.

The newest version is fetched from GitHub and taken only by fast-forward, so nothing of
the user's is merged, overwritten or thrown away (their runs, drawings and settings are
outside git). The app then starts again on the new version, through uv, which first
brings the installed packages in line with it. The installed Windows app is updated with
a new installer instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import paths


class UpdateError(Exception):
    """An update that could not be made; nothing was changed."""


def _git(root: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          timeout=timeout, check=False,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})  # never wait for a login


def head(root: Path) -> str | None:
    try:
        r = _git(root, "rev-parse", "--short", "HEAD", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


# The version this process started with: code changed on disk since then runs only after
# a restart.
RUNNING = None if paths.FROZEN else head(paths.SOURCE_ROOT)


def supported(root: Path) -> tuple[bool, str]:
    if paths.FROZEN:
        return False, "This is the installed app: a new version comes as a new installer."
    if not (root / ".git").exists():
        return False, "This copy of CrossingCount is not a git checkout."
    if shutil.which("git") is None:
        return False, "git is not installed on this computer."
    return True, ""


def status(root: Path | None = None, running: str | None = RUNNING, fetch: bool = True
           ) -> dict[str, Any]:
    """Is there a newer version on GitHub (behind, with its changes), or one already on this
    computer that is not the one running (restart)?"""
    root = root or paths.SOURCE_ROOT
    ok, reason = supported(root)
    out: dict[str, Any] = {"supported": ok, "reason": reason, "behind": 0, "changes": [],
                           "restart": False, "notes": []}
    if not ok:
        return out
    try:
        if fetch:
            f = _git(root, "fetch", "--quiet", "origin")
            if f.returncode != 0:
                out["notes"].append("GitHub could not be reached, so newer versions there are "
                                    "not known: " + f.stderr.strip()[:200])
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10).stdout.strip()
        upstream = f"origin/{branch}"
        count = _git(root, "rev-list", "--count", f"HEAD..{upstream}", timeout=10)
        out["behind"] = int(count.stdout.strip() or 0) if count.returncode == 0 else 0
        if out["behind"]:
            log = _git(root, "log", "--format=%s", f"HEAD..{upstream}", timeout=10)
            out["changes"] = log.stdout.splitlines()[:10]
        now = head(root)
        out["head"], out["running"] = now, running
        out["restart"] = bool(running and now and running != now)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        out["notes"].append(f"git could not be asked: {e}")
    return out


def apply(root: Path | None = None) -> str:
    """Take the newest version, by fast-forward only. Returns the version now on disk."""
    root = root or paths.SOURCE_ROOT
    ok, reason = supported(root)
    if not ok:
        raise UpdateError(reason)
    r = _git(root, "pull", "--ff-only", "--quiet", timeout=180)
    if r.returncode != 0:
        said = (r.stderr or r.stdout).strip()[:300]
        raise UpdateError(f"The update could not be applied, and nothing was changed: {said}")
    return head(root) or ""


def _uv() -> str | None:
    found = os.environ.get("UV") or shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for p in (Path("/opt/homebrew/bin/uv"), home / ".local/bin/uv", home / ".cargo/bin/uv",
              Path("/usr/local/bin/uv")):
        if p.is_file():
            return str(p)
    return None


def restart(argv: list[str] | None = None) -> None:
    """Start the app again on the new version, in this same process (the page reloads
    itself, so no new browser tab)."""
    argv = list(sys.argv if argv is None else argv)
    args = [a for a in argv[1:] if a != "--no-browser"] + ["--no-browser"]
    script = str(paths.SOURCE_ROOT / "wizard.py")
    uv = _uv()
    if uv:
        os.execv(uv, [uv, "run", "--directory", str(paths.SOURCE_ROOT), script, *args])
    os.execv(sys.executable, [sys.executable, script, *args])
