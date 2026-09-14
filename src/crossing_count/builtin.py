"""What GitHub's build puts inside the app: RetailNext brands, and a token for update notices.

Filled from the repository's secrets when GitHub builds the app (packaging/build_info.py),
so a colleague who installs it can open every built-in brand's stores, and hears of new
versions, with nothing to type. A copy run from the project folder has none of this.

Not secret from whoever has the app: a key built in can be dug out of it. A lost laptop
means changing the key in RetailNext, then building again.
"""

from __future__ import annotations

import base64
import functools
import json
from pathlib import Path
from typing import Any

FILE = Path(__file__).with_name("builtin.dat")
BUILD = Path(__file__).with_name("build.json")


@functools.lru_cache(maxsize=1)
def build() -> dict[str, Any] | None:
    """Which of GitHub's builds this is: platform (mac, win), build number, commit, repository."""
    try:
        info = json.loads(BUILD.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    ok = isinstance(info, dict) and {"platform", "build", "commit", "repo"} <= info.keys()
    return info if ok else None


@functools.lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    try:
        raw = json.loads(base64.b64decode(FILE.read_bytes()).decode("utf-8"))
    except (OSError, ValueError):  # none built in (binascii and JSON errors are ValueErrors)
        return {}
    return raw if isinstance(raw, dict) else {}


def retailnext() -> dict[str, dict[str, str]]:
    """Built-in brands: name -> {"access_key", "secret_key"}."""
    brands = _data().get("retailnext")
    if not isinstance(brands, dict):
        return {}
    return {str(name).lower(): {"access_key": str(k["access_key"]), "secret_key": str(k["secret_key"])}
            for name, k in brands.items()
            if isinstance(k, dict) and k.get("access_key") and k.get("secret_key")}


def github_token() -> str | None:
    token = _data().get("github_token")
    return str(token) if token else None
