"""The sensor's 15-minute intervals, and how far its count is from the verified one.

Small shared pieces, used by the wizard (its интervals and the report's accuracy card), by
the benchmark (reading counts kept in the older CSV layout) and by manual.py's range
helpers. The CSV and HTML writers that once lived here belonged to the standalone export
command, which the wizard replaced; validation.py and report_pptx.py write the outputs now.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

CSV_COLUMNS = ["video_time", "video_seconds", "clock_time", "direction", "tags"]
INTERVAL_MIN = 15


def interval_start(t: datetime, minutes: int = INTERVAL_MIN) -> datetime:
    """The start of the sensor's interval holding this moment."""
    return t.replace(minute=t.minute - t.minute % minutes, second=0, microsecond=0)


def sensor_accuracy(sensor: int | None, truth: int) -> dict[str, Any]:
    """One comparison: the system's count against the verified one. 100% means they agree;
    below MIN_VERIFIED_FOR_PCT crossings the report prints the difference instead of this
    percentage (validation.quote)."""
    if sensor is None:
        return {"sensor": None, "verified": truth, "error": None, "error_pct": None,
                "accuracy_pct": None}
    err = sensor - truth
    if truth == 0:  # nobody really crossed: right only if the sensor also says 0
        pct, acc = None, (100.0 if sensor == 0 else None)
    else:
        pct = round(100.0 * err / truth, 1)
        acc = round(max(0.0, 100.0 - abs(100.0 * err / truth)), 1)
    return {"sensor": sensor, "verified": truth, "error": err, "error_pct": pct,
            "accuracy_pct": acc}
