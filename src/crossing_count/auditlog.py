"""The audit log: every meaningful human action, in order, in a file that shows if it was
edited.

One line per action in <data>/audit/audit.jsonl: when, who (the name typed in the app, and
the computer's login), what, on which footage, the value before and after, and why when
given. Each line carries the SHA-256 of the line before it, so changing, removing or
inserting a line breaks the chain from there on, and verify() says where. The app only
ever appends. A chain rewritten from the start would verify again, which is why every
finalised validation records the log's latest hash (runs.py): the log must still contain
that hash for the validations made from it.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import network, paths

FILE = "audit.jsonl"
GENESIS = "0" * 64
_lock = threading.Lock()


def path(root: Path | None = None) -> Path:
    return (root or paths.data_root()) / "audit" / FILE


def _digest(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _last(p: Path) -> tuple[int, str]:
    """The last entry's number and hash (0 and the genesis hash for a new log)."""
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = [x for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
    except OSError:
        return 0, GENESIS
    if not lines:
        return 0, GENESIS
    last = json.loads(lines[-1])
    return int(last["seq"]), str(last["hash"])


def _login() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return ""


def append(action: str, *, user: str = "", obj: dict[str, Any] | None = None,
           before: Any = None, after: Any = None, reason: str | None = None,
           root: Path | None = None, **info: Any) -> dict[str, Any]:
    """Add one action to the end of the log (written to disk before this returns)."""
    with _lock:
        p = path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        seq, prev = _last(p)
        entry: dict[str, Any] = {
            "seq": seq + 1, "at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "user": user, "login": _login(), "action": action, "object": obj,
            "before": before, "after": after, "reason": reason, "info": info or None, "prev": prev}
        if (client := network.CLIENT.get()) is not None:  # done by someone on the network
            entry["from"] = client
        entry["hash"] = _digest(entry)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry


def verify(root: Path | None = None) -> dict[str, Any]:
    """Walk the chain: is every line intact and in order? Where does it break?"""
    p = path(root)
    prev, seq, n = GENESIS, 0, 0
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"entries": 0, "intact": True, "broken_at": None, "problem": None, "head": GENESIS}
    for k, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            return {"entries": n, "intact": False, "broken_at": k, "head": prev,
                    "problem": f"line {k} is not a readable entry"}
        if e.get("prev") != prev or int(e.get("seq", -1)) != seq + 1 or e.get("hash") != _digest(e):
            return {"entries": n, "intact": False, "broken_at": k, "head": prev,
                    "problem": f"line {k} (entry {e.get('seq')}) was changed, removed or inserted"}
        prev, seq, n = str(e["hash"]), seq + 1, n + 1
    return {"entries": n, "intact": True, "broken_at": None, "problem": None, "head": prev}


def head(root: Path | None = None) -> str:
    return _last(path(root))[1]


def contains(hash_: str, root: Path | None = None) -> bool:
    """Is this hash (a finalised validation's record of the log) still in the log?"""
    try:
        return f'"hash": "{hash_}"' in path(root).read_text(encoding="utf-8")
    except OSError:
        return False


def recent(n: int = 50, root: Path | None = None) -> list[dict[str, Any]]:
    try:
        lines = path(root).read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
