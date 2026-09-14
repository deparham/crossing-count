"""Written into the app by GitHub's build: which build it is, and what is built in.

    uv run python packaging/build_info.py mac|win

Reads GITHUB_RUN_NUMBER, GITHUB_SHA and GITHUB_REPOSITORY, and the repository secrets
RETAILNEXT_BRANDS (set from the page: "Build these brands into the apps") and
UPDATE_TOKEN (a GitHub token that can read the repository) when they are set. Writes
src/crossing_count/build.json and builtin.dat, which git ignores. Never prints a key.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "src" / "crossing_count"


def main(argv: list[str], pkg: Path = PKG) -> int:
    if len(argv) != 1 or argv[0] not in ("mac", "win"):
        print(__doc__)
        return 2
    info = {"platform": argv[0], "build": int(os.environ["GITHUB_RUN_NUMBER"]),
            "commit": os.environ["GITHUB_SHA"], "repo": os.environ["GITHUB_REPOSITORY"]}
    (pkg / "build.json").write_text(json.dumps(info), encoding="utf-8")
    inside: dict[str, object] = {}
    brands_text = os.environ.get("RETAILNEXT_BRANDS", "").strip()
    if brands_text:
        brands = json.loads(brands_text)
        if not isinstance(brands, dict) or not all(
                isinstance(k, dict) and k.get("access_key") and k.get("secret_key")
                for k in brands.values()):
            print("RETAILNEXT_BRANDS is not {brand: {access_key, secret_key}}: nothing built in")
            return 1
        inside["retailnext"] = brands
    token = os.environ.get("UPDATE_TOKEN", "").strip()
    if token:
        inside["github_token"] = token
    target = pkg / "builtin.dat"
    if inside:
        target.write_bytes(base64.b64encode(json.dumps(inside).encode("utf-8")))
    else:
        target.unlink(missing_ok=True)
    names = ", ".join(sorted(inside.get("retailnext", {}))) or "none"  # type: ignore[arg-type]
    print(f"{info['platform']} build {info['build']} of {info['repo']}; brands built in: {names}; "
          f"update token: {'yes' if token else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
