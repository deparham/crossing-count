"""Which build of CrossingCount made a result: its version, and the git commit in a checkout."""

from __future__ import annotations

import functools
import subprocess

from . import __version__, paths


@functools.lru_cache(maxsize=1)
def app_version() -> str:
    """"0.1.0", or "0.1.0 (a834778)" from a git checkout ("…, changed" with edits not committed)."""
    if paths.FROZEN or not (paths.SOURCE_ROOT / ".git").exists():
        return __version__

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(paths.SOURCE_ROOT), *args], capture_output=True,
                              text=True, timeout=5, check=False)

    try:
        head = git("rev-parse", "--short", "HEAD")
        changed = git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        return __version__
    if head.returncode != 0 or not head.stdout.strip():
        return __version__
    return f"{__version__} ({head.stdout.strip()}{', changed' if changed.stdout.strip() else ''})"
