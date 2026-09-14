from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def write_json_atomic(path: str | Path, obj: Any) -> None:
    """Write JSON via a temp file and rename, so a crash never leaves a torn file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def slugify(text: str, max_len: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:max_len].rstrip("-") or "video"


def default_run_dir(video: str | Path, sample_s: float | None) -> Path:
    name = slugify(Path(video).stem)
    if sample_s is not None:
        name += f"__sample{sample_s:g}"
    return Path("runs") / name


def same_store(site: str, store: tuple[str, ...]) -> bool:
    """Does a drawing's site name one of the store's names (its code, its full name)? Either
    written whole, or as the code followed by more ("392 Perri Cutten Armadale")."""
    s = " ".join(site.lower().split())
    names = [" ".join(n.lower().split()) for n in store if n.strip()]
    return any(s == n or s.startswith(n + " ") for n in names)


def fmt_hms(seconds: float) -> str:
    s = max(0.0, seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:05.2f}"
