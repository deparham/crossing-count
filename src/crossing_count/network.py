"""Letting people on this network use CrossingCount, each on their own validation.

Off unless someone at this computer switches it on. Then a second listener answers on
every network address of this computer (port NETWORK_PORT), beside the usual one that only
this computer can reach. Another computer gets in with the access code, made new each
time sharing starts, and after that a cookie that only this server's pages carry: a web
page from anywhere else can neither guess the code nor send the cookie.

What stays with this computer, whoever asks over the network (wizard_app.HOST_ONLY): the
RetailNext keys and their management, updates, settings, quitting, opening folders on this
screen, and picking a file anywhere on its disk (people on the network see the footage
folders only). Counts run one at a time on this computer (jobs.py), whoever started them.
"""

from __future__ import annotations

import contextvars
import hmac
import ipaddress
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

NETWORK_PORT = 8781
COOKIE = "cc_access"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: read aloud or typed
MAX_FAILURES = 10  # wrong codes in FAIL_WINDOW_S before joining is refused for a while
FAIL_WINDOW_S = 600.0
LOOPBACK = frozenset({"127.0.0.1", "::1", "testclient"})  # the test client is this computer

# the network address of the person making this request; None on this computer
CLIENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("cc_client", default=None)
# whose validation this request works on: "local" on this computer, else the person's access
SESSION: contextvars.ContextVar[str] = contextvars.ContextVar("cc_session", default="local")


def on_this_computer(address: str | None) -> bool:
    if address is None or address in LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_loopback
    except ValueError:
        return False


def new_code() -> str:
    """Eight characters, about 40 bits, in two groups of four: XXXX-XXXX."""
    chars = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def _plain(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


class Access:
    """The access code and the people let in with it."""

    def __init__(self) -> None:
        self.code = new_code()
        self._tokens: dict[str, float] = {}  # token -> when it was given
        self._failures: deque[float] = deque()
        self._lock = threading.Lock()

    def locked_out(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._failures and now - self._failures[0] > FAIL_WINDOW_S:
                self._failures.popleft()
            return len(self._failures) >= MAX_FAILURES

    def join(self, code: str, now: float | None = None) -> str | None:
        """A token for the cookie when the code is right; None when it is wrong, or when too
        many wrong codes were tried lately (then even the right one waits)."""
        now = time.monotonic() if now is None else now
        if self.locked_out(now):
            return None
        if not hmac.compare_digest(_plain(code).encode(), _plain(self.code).encode()):
            with self._lock:
                self._failures.append(now)
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = now
        return token

    def admitted(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return any(hmac.compare_digest(token, t) for t in self._tokens)


def _local_host_name() -> str:
    """The name other computers can find this one by: its Bonjour name on a Mac."""
    if sys.platform == "darwin":
        try:
            got = subprocess.run(["/usr/sbin/scutil", "--get", "LocalHostName"],
                                 capture_output=True, text=True, timeout=2, check=False)
            if got.returncode == 0 and got.stdout.strip():
                return f"{got.stdout.strip()}.local"
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname()


def _private_ipv4() -> list[str]:
    found: set[str] = set()
    try:  # the address used to reach other networks; connecting a UDP socket sends nothing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        found.update(str(i[4][0]) for i in socket.getaddrinfo(socket.gethostname(), None,
                                                               socket.AF_INET))
    except OSError:
        pass
    return sorted(a for a in found if ipaddress.ip_address(a).is_private
                  and not ipaddress.ip_address(a).is_loopback)


def addresses(port: int) -> list[str]:
    """Where people on the network open CrossingCount: by name first, then by address."""
    return [f"http://{h}:{port}/" for h in [_local_host_name(), *_private_ipv4()]]


def within(path: Path, roots: list[Path]) -> bool:
    """Is this file inside one of these folders (after following links)?"""
    try:
        p = path.expanduser().resolve()
    except OSError:
        return False
    for r in roots:
        try:
            if p.is_relative_to(r.expanduser().resolve()):
                return True
        except OSError:
            continue
    return False


@dataclass
class Sharing:
    """The listener for the network, started and stopped from the page."""

    app: Any
    port: int = NETWORK_PORT
    bind: str = "0.0.0.0"  # every network address of this computer
    access: Access | None = None
    error: str | None = None
    started_at: float | None = None
    urls: list[str] = field(default_factory=list)  # this computer's addresses, found at start
    _server: Any = None
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def on(self) -> bool:
        return self._server is not None and bool(self._server.started)

    def start(self, wait_s: float = 10.0) -> bool:
        """Listen on the network with a new access code. False, with the reason in error,
        when the port is taken or the listener does not come up."""
        import uvicorn

        with self._lock:
            if self.on:
                return True
            self.error = None
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind((self.bind, self.port))
                except OSError as exc:
                    self.error = f"Port {self.port} is in use by another program ({exc.strerror})."
                    return False
            self.access = Access()
            server = uvicorn.Server(uvicorn.Config(self.app, host=self.bind, port=self.port,
                                                   log_level="warning"))
            thread = threading.Thread(target=server.run, daemon=True, name="cc-network")
            thread.start()
            deadline = time.monotonic() + wait_s
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    server.should_exit = True
                    self.error = f"Could not listen on port {self.port}."
                    return False
                time.sleep(0.05)
            self._server, self._thread, self.started_at = server, thread, time.time()
            self.urls = addresses(self.port)
            return True

    def stop(self) -> None:
        """Stop listening on the network. People already in lose access; the code is spent."""
        with self._lock:
            server, thread = self._server, self._thread
            self._server = self._thread = None
            self.access, self.started_at = None, None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)

    def admitted(self, token: str | None) -> bool:
        return self.on and self.access is not None and self.access.admitted(token)

    def names(self) -> frozenset[str]:
        """The host names in the addresses given out (lower case)."""
        return frozenset((urlsplit(u).hostname or "").lower() for u in self.urls) - {""}

    def status(self) -> dict[str, Any]:
        if not self.on or self.access is None:
            return {"on": False, "port": self.port, "error": self.error}
        return {"on": True, "port": self.port, "code": self.access.code, "addresses": self.urls,
                "links": [f"{u}?code={self.access.code}" for u in self.urls],
                "since": self.started_at, "error": None}
