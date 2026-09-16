"""Running gate.py and detect.py for a validation, and following what they say.

The wizard starts them as child processes of this same program (a sub-command when it is
installed), reads their output line by line, and turns it into something a person can
watch: which stage, which camera, how far through, how fast.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .. import paths
from ..util import fmt_hms

if TYPE_CHECKING:
    from .state import Wizard

ROOT = paths.SOURCE_ROOT  # the project folder, when running from source

_GATE = re.compile(r"gating (\d+):(\d+):([\d.]+) / (\d+):(\d+):([\d.]+)")
_TRACK = re.compile(r"tracking\s+([\d.]+)% of active time\s+\(\s*([\d.]+)x realtime\)")
_CAMERA = re.compile(r"^\s*(\S.*?): \w+ detector,")


def _secs(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


@dataclass
class Progress:
    """What gate.py and detect.py are doing, read from their output."""

    cameras: int = 1
    stage: str = "starting"  # starting, gate, detect, done
    camera: str = ""
    seen: list[str] = field(default_factory=list)
    stage_pct: float = 0.0
    speed: float | None = None
    message: str = "Starting"
    log: list[str] = field(default_factory=list)

    def overall(self) -> float:
        """Percent of the whole job: the gate is quick (5%), detection is the rest."""
        if self.stage == "done":
            return 100.0
        if self.stage == "gate":
            return 5.0 * self.stage_pct / 100.0
        if self.stage == "detect":
            per = 95.0 / max(1, self.cameras)
            return 5.0 + per * (max(0, len(self.seen) - 1) + self.stage_pct / 100.0)
        return 0.0

    def feed(self, text: str) -> None:
        line = text.strip()
        if not line or line.startswith("objc["):
            return
        m = _GATE.search(line)
        if m:
            done, total = _secs(m[1], m[2], m[3]), _secs(m[4], m[5], m[6])
            self.stage = "gate"
            self.stage_pct = min(100.0, 100.0 * done / total) if total else 0.0
            self.message = (f"Finding movement near the line: {fmt_hms(done)[:8]} of "
                            f"{fmt_hms(total)[:8]}")
            return
        m = _TRACK.search(line)
        if m:
            self.stage, self.stage_pct, self.speed = "detect", float(m[1]), float(m[2])
            self.message = (f"Detecting and tracking people on {self.camera}: "
                            f"{self.stage_pct:.0f}% ({self.speed:.1f}x realtime)")
            return
        m = _CAMERA.match(text)
        if m:
            self.stage, self.camera, self.stage_pct = "detect", m[1], 0.0
            if m[1] not in self.seen:
                self.seen.append(m[1])
            self.message = f"Detecting and tracking people on {self.camera}"
        self.log.append(line)
        del self.log[:-300]


class _Job:
    """Runs the commands one after another in a thread, feeding their output to Progress."""

    def __init__(self, commands: list[list[str]], progress: Progress,
                 finish: Callable[[str, str | None, list[str]], None]) -> None:
        self.commands = commands
        self.progress = progress
        self._finish = finish
        self.started = time.monotonic()
        self.stopped = False
        self.running = True
        self._proc: subprocess.Popen[bytes] | None = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        status: str = "done"
        error: str | None = None
        cwd = paths.data_root()
        cwd.mkdir(parents=True, exist_ok=True)
        for cmd in self.commands:
            if self.stopped:
                break
            name = Path(cmd[1]).name if len(cmd) > 1 else cmd[0]
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as exc:
                status, error = "failed", f"could not start {name}: {exc}"
                break
            self._proc = proc
            assert proc.stdout is not None
            fd, pending = proc.stdout.fileno(), b""
            while True:
                data = os.read(fd, 4096)
                if not data:
                    break
                parts = re.split(rb"[\r\n]", pending + data)
                pending = parts.pop()
                for part in parts:
                    self.progress.feed(part.decode("utf-8", "replace"))
            if pending:
                self.progress.feed(pending.decode("utf-8", "replace"))
            code = proc.wait()
            if self.stopped:
                break
            if code != 0:
                status, error = "failed", (f"{name} stopped with an error (exit code {code}); "
                                           f"the log says why")
                break
        if self.stopped:
            status, error = "stopped", None
        if status == "done":
            self.progress.stage, self.progress.message = "done", "Finished"
        self.running = False
        self._finish(status, error, self.progress.log[-40:])

    def stop(self) -> None:
        self.stopped = True
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()


def tool(name: str) -> list[str]:
    """How to start one of the pipeline scripts: a sub-command of this program when installed."""
    if paths.FROZEN:
        return [sys.executable, name]
    return [sys.executable, str(ROOT / f"{name}.py")]


def pipeline_commands(w: Wizard) -> list[list[str]]:
    """gate.py then detect.py for the chosen cameras, each pinned to its picture."""
    cams = w.state["cameras"]
    cfgs = [c["config"] for c in cams]
    tiles = [a for c in cams for a in ("--tile", f"{c['sensor']}={c['picture']}")]
    out = ["--out-dir", str(w.run_dir)]
    return [
        [*tool("gate"), str(w.video), *cfgs, *tiles, *out],
        [*tool("detect"), str(w.video), *cfgs, "--model", w.state["model"], "--record", *out],
    ]
