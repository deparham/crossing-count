"""The version a report and a count record."""

from __future__ import annotations

import tomllib

from crossing_count import __copyright__, __version__, paths
from crossing_count.version import app_version


def test_the_package_version_is_the_projects() -> None:
    project = tomllib.loads((paths.SOURCE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == project["project"]["version"]
    assert app_version().startswith(__version__)
    assert __copyright__ == "© 2026 Parham Forozan"
    page = (paths.SOURCE_ROOT / "src" / "crossing_count" / "web" / "wizard.html").read_text()
    assert "© 2026 Parham Forozan" in page
