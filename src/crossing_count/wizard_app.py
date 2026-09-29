"""Local count-wizard page. Served from 127.0.0.1; with sharing switched on, also to people
on this network with the access code (network.py), each on their own validation.

The drawing step is the setup page itself, mounted at /draw/<id>/ for the chosen
video and shown in a frame.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from av.error import FFmpegError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from starlette.routing import Mount

from . import (
    auditlog,
    engagement,
    gold,
    graph,
    independence,
    network,
    paths,
    releases,
    runs,
    sampling,
    sensors,
    updates,
    validation,
)
from . import overlay as ov
from . import retailnext as rn
from .examples import check_folder
from .localweb import WEB_DIR, local_only, video_range
from .util import default_run_dir
from .webapp import Setup, create_app
from .wizard import Wizard, WizardError, default_folders, list_videos

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
LOCK_IDLE_S = 15 * 60  # footage left this long (and not counting) can be opened by someone else
# What stays with the computer running CrossingCount, whoever asks over the network: its
# RetailNext keys, updates, settings, quitting, its own screen (Finder), and the shared
# records' irreversible steps. tests/test_network.py checks each is a real route.
HOST_ONLY = frozenset({
    ("POST", "/api/retailnext/connect"), ("POST", "/api/retailnext/reconnect"),
    ("POST", "/api/retailnext/forget"), ("POST", "/api/share/brands"),
    ("POST", "/api/share/token"), ("POST", "/api/update"), ("POST", "/api/update/token"),
    ("POST", "/api/quit"), ("POST", "/api/settings"), ("POST", "/api/reveal"),
    ("POST", "/api/runs/reveal"), ("POST", "/api/audit/marks/reclassify"),
    ("POST", "/api/gold/freeze"), ("POST", "/api/network"),
    # the Microsoft account is this computer's, exactly as its RetailNext keys are
    ("POST", "/api/microsoft"), ("POST", "/api/microsoft/signin"),
    ("POST", "/api/microsoft/forget"),
})


class OpenIn(BaseModel):
    path: str


class RunIn(BaseModel):
    confirm: bool = False  # run a count that was already checked (the check is kept)


EXPORT_POLL_S = 5.0  # how often to ask RetailNext whether an export is ready
EXPORT_WAIT_S = 1800.0  # and for how long, before giving up
CALIBRATE_S = 60.0  # how much of the window to export again with marks, to draw the line on


class ForgetIn(BaseModel):
    subscription: str


class TokenIn(BaseModel):
    token: str  # kept in the credential store (or GitHub's secrets) only; never sent back


class GoldSaveIn(BaseModel):
    tags: list[str] = []
    notes: str = ""
    lighting: str = "normal"
    occlusion: str = "none"


class GoldFreezeIn(BaseModel):
    note: str = ""


class RunRef(BaseModel):
    id: str  # a validation's ID, CC-VAL-...


class CsvIn(BaseModel):
    name: str  # the file's name, as the report names the source
    text: str


class RulesIn(BaseModel):
    children: str = "count"  # or "exclude", as the sensor is set up (Ground Truth Spec, s. 4)
    staff: str = "count"


class GoldScoreIn(BaseModel):
    which: str = "development"  # or "test": the held-out stores, for a final check only
    note: str = ""
    clean_only: bool = False  # leave out clips counted on marked footage (the test set always does)


def _restart() -> None:
    """Start again on the version just updated to."""
    updates.restart()


def _stop_server() -> None:
    """Close the app from its page. Everything is saved as it happens, so there is nothing
    left to write; a count or download still running stops with it."""
    os._exit(0)


class ConnectIn(BaseModel):
    subscription: str
    access_key: str
    secret_key: str  # kept in the credential store only; never sent back or logged


class BusiestIn(BaseModel):
    code: str  # the store's code, e.g. CN-123
    date: str  # YYYY-MM-DD
    minutes: int = 15
    direction: str = "out"  # in, out or both: the busiest for the traffic validated
    mode: str = sampling.DEFAULT_MODE  # peak (with a control window), stratified or random
    seed: int | None = None  # the random draws' seed; a new one is drawn and recorded if none


class EngagementIn(BaseModel):
    ids: list[str]  # finalised validation IDs, all of one store


class PictureIn(BaseModel):
    picture: int


def _bare(code: str) -> str:
    """A store code without its brand ("rag/CN-123" -> "CN-123")."""
    return code.rpartition("/")[2].strip().upper()


class DownloadIn(BaseModel):
    code: str
    date: str
    start: str  # HH:MM, the store's own time
    until: str
    marks: bool = False  # with RetailNext's own marks (for a count by hand)
    both: bool = False  # the same window twice: clean to count on, marked to count by hand


class QueueIn(BaseModel):
    """Several stores' footage at once: each store's windows, in the order the stores came."""

    codes: list[str]
    date: str
    minutes: int = 15
    direction: str = "in"
    mode: str = "peak"  # the sampling rule
    seed: int | None = None
    marks: bool = False
    both: bool = False
    count: str = "auto"  # how it will be counted, so opening one needs nothing remembered
    direction_for: str = ""  # the traffic to validate, set on the wizard when it is opened
    per_store: int = 1  # windows queued per store: the busiest one, unless more are asked for


class DirectionIn(BaseModel):
    direction: str


class SensorIn(BaseModel):
    values: dict[str, int | None] | None = None  # the total per direction
    intervals: dict[str, dict[str, int | None]] | None = None  # or per 15-minute interval
    cameras: dict[str, dict[str, int | None]] | None = None  # each camera's own, optional


class ModelIn(BaseModel):
    model: str


class AnswerIn(BaseModel):
    id: str
    answer: str | None = None
    people: int = 1  # a "yes" for a group crossing together


class AddIn(BaseModel):
    camera: str
    t: float
    direction: str
    range: str | None = None


class IndexIn(BaseModel):
    index: int


class WatchIn(BaseModel):
    mode: str | None = None


class WatchedIn(BaseModel):
    range: str
    done: bool = True


class StoreIn(BaseModel):
    name: str | None = None
    code: str | None = None
    location: str | None = None
    report_date: str | None = None
    operator: str | None = None


class ModeIn(BaseModel):
    mode: str


class MarksIn(BaseModel):
    marks: bool


class TotalStepIn(BaseModel):
    camera: str
    direction: str
    step: int = 1


class TotalSetIn(BaseModel):
    camera: str
    direction: str
    value: int | None = None


class TotalNotesIn(BaseModel):
    notes: str = ""


class TotalDoneIn(BaseModel):
    done: bool = True
    whole_clip: bool | None = None


class NamedCamerasIn(BaseModel):
    cameras: list[dict[str, Any]]


class HandAddIn(BaseModel):
    camera: str
    t: float
    direction: str
    uncertain: bool = False
    note: str = ""


class HandEditIn(BaseModel):
    id: int
    t: float | None = None
    direction: str | None = None
    uncertain: bool | None = None
    note: str | None = None
    reason: str = ""  # why it changed: kept in the logs


class AdjudicateIn(BaseModel):
    by: str  # who settled the disagreements
    decisions: list[dict[str, Any]]  # {camera, t, decision: in | out | none | uncertain, reason}


class HandCameraIn(BaseModel):
    camera: str


class HandIdIn(BaseModel):
    id: int


class HandWatchedIn(BaseModel):
    camera: str
    start: float
    end: float


class HandPositionIn(BaseModel):
    camera: str
    t: float


class DoneIn(BaseModel):
    done: bool = True


class DiagnosisIn(BaseModel):
    key: str  # camera@seconds, as the comparison names it
    cause: str = ""  # one of wizard.CAUSES, or empty to remove it
    note: str = ""


class SettingsIn(BaseModel):
    examples_dir: str
    delivery_dir: str | None = None  # where finished validations are delivered for the customer


class DeliverIn(BaseModel):
    footage: bool = False  # the footage goes only when this says so, each time


class MicrosoftIn(BaseModel):
    tenant: str = ""  # Directory (tenant) ID; not a secret
    client_id: str = ""  # Application (client) ID; not a secret
    host: str = ""  # e.g. acme.sharepoint.com, when the sheet's own links do not say


class NetworkIn(BaseModel):
    on: bool


@dataclass
class _Current:
    """One person's work (this computer's, or someone's on the network), and, in one shared
    copy, what belongs to everyone (store lists, the update, gold scoring)."""

    wizard: Wizard | None = None
    setup: Setup | None = None
    draw: str | None = None
    calibrate: str | None = None  # where this person's calibration drawing page is mounted
    rn_nodes: dict[str, list[dict[str, Any]]] | None = None  # per subscription, fetched once
    rn_job: dict[str, Any] | None = None  # the footage being exported and downloaded
    rn_queue: list[dict[str, Any]] = field(default_factory=list)  # footage still to fetch
    rn_working: bool = False  # a worker is draining the queue
    rn_plan: dict[str, Any] | None = None  # the windows last found: a download records its own
    update_job: dict[str, Any] | None = None  # the installed app downloading its new version
    gold_job: dict[str, Any] | None = None  # the automatic count being scored on gold clips
    address: str | None = None  # on the network; None on this computer
    operator: str | None = None  # the name typed by someone on the network
    last_seen: float = 0.0

    def who(self) -> str:
        name = self.operator or (self.wizard.state["store"].get("operator")
                                 if self.wizard is not None else None)
        where = "this computer" if self.address is None else self.address
        return f"{name} ({where})" if name else f"someone at {where}"


def create_wizard_app(sites_dir: Path | None = None, runs_root: Path | None = None,
                      folders: list[Path] | None = None, logo: Path | None = None,
                      commands: Callable[[Wizard], list[list[str]]] | None = None,
                      network_port: int = network.NETWORK_PORT) -> FastAPI:
    sites = sites_dir or paths.sites_dir()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sharing = network.Sharing(app, network_port)
    app.state.sharing = sharing  # the launcher switches it on at start when asked
    # only its own pages, on this computer; with sharing on, people with the access code
    local_only(app, sharing, HOST_ONLY)
    glob = _Current()  # everyone's: RetailNext's store lists, the update, gold scoring
    sessions: dict[str, _Current] = {}  # each person's own: "local" is this computer
    lock = threading.Lock()  # the sessions, and the pages mounted for them

    def me() -> _Current:
        """The validation this request works on: its person's own."""
        with lock:
            mine = sessions.setdefault(network.SESSION.get(), _Current())
            mine.address, mine.last_seen = network.CLIENT.get(), time.time()
            return mine

    def remote() -> bool:
        return network.CLIENT.get() is not None

    def unmount(where: str | None) -> None:
        if where:
            app.router.routes[:] = [r for r in app.router.routes
                                    if not (isinstance(r, Mount) and r.path == where.rstrip("/"))]

    def drop(person: _Current) -> None:
        """Close a person's footage (it is saved as it goes): someone else may open it."""
        unmount(person.draw)
        unmount(person.calibrate)
        person.wizard = person.setup = None
        person.draw = person.calibrate = None

    def holder(path: Path, mine: _Current) -> str | None:
        """Who else has this footage's validation open and is still at it (or counting). A
        validation lives in a folder named after the video, wherever the file is (Wizard)."""
        target = ((runs_root or paths.data_root()) / default_run_dir(path, None)).resolve()
        with lock:
            for other in sessions.values():
                if (other is mine or other.wizard is None
                        or other.wizard.run_dir.resolve() != target):
                    continue
                if (other.wizard.job_status()["status"] == "running"
                        or time.time() - other.last_seen < LOCK_IDLE_S):
                    return other.who()
                drop(other)
        return None

    def footage_roots() -> list[Path]:
        """Where people on the network may pick footage: the footage folders, and downloads."""
        return [*(folders or default_folders()), paths.data_root() / "footage"]

    def operator_name() -> str:
        if remote():
            return me().operator or ""
        return str(paths.load_settings().get("operator") or "")
    from .heads_app import create_label_app  # the head-marking page, at /label/

    app.mount("/label", create_label_app(folders=folders, runs_root=runs_root))

    def wiz() -> Wizard:
        mine = me()
        if mine.wizard is None:
            raise HTTPException(409, "Choose the footage first.")
        return mine.wizard

    def setup() -> Setup:
        mine = me()
        if mine.setup is None:
            raise HTTPException(409, "Choose the footage first.")
        return mine.setup

    def public() -> dict[str, Any]:
        return {**wiz().public(), "draw_url": me().draw, "on_host": not remote(),
                "retailnext": bool(rn.subscriptions()),
                "logo": str(logo) if logo is not None and logo.is_file() else None,
                "pictures": [t.as_dict() for t in setup().tiles]}

    def run(change: Callable[[], object]) -> dict[str, Any]:
        try:
            change()
        except WizardError as exc:
            raise HTTPException(400, str(exc)) from exc
        return public()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "wizard.html").read_text(encoding="utf-8")

    @app.get("/api/videos")
    def videos(folder: str | None = None) -> dict[str, Any]:
        where = [Path(folder).expanduser()] if folder else (folders or default_folders())
        if remote() and not all(network.within(f, footage_roots()) for f in where):
            raise HTTPException(403, "On the network, footage comes from the footage folders of "
                                     "the computer running CrossingCount.")
        return {"folders": [str(f) for f in where], "videos": list_videos(where)}

    @app.post("/api/open")
    def open_video(o: OpenIn) -> dict[str, Any]:
        path = Path(o.path).expanduser()
        if remote() and not network.within(path, footage_roots()):
            raise HTTPException(403, "On the network, footage comes from the footage folders of "
                                     "the computer running CrossingCount.")
        if not path.is_file():
            raise HTTPException(400, f"There is no file at {path}")
        mine = me()
        if (someone := holder(path, mine)) is not None:
            raise HTTPException(409, f"{path.name} is open for {someone}. Two people cannot work "
                                     f"on the same footage at once: it can be opened here once "
                                     f"they open other footage, or after "
                                     f"{LOCK_IDLE_S // 60} minutes without using it.")
        try:
            w = Wizard(path, sites, runs_root, commands)
            # how many cameras this footage holds, when it is a download that says so: the
            # channels were asked for by name and kept beside the file. Worked out before the
            # pictures are split rather than read afterwards, which is what left a two-camera
            # export counted as one (identify_download below only names them).
            s = Setup(w.video, sites, expect=_channels_in(path))
        except (WizardError, ValueError, OSError, FFmpegError) as exc:
            raise HTTPException(400, f"Could not open {path.name}: {exc}") from exc
        name = operator_name()
        final = w.state.get("final")  # a finalised validation opens as it was kept
        if name and not w.state["store"].get("operator") and not final:
            w.set_store(operator=str(name))
        if not final:  # what the pictures show, not only what someone answers (independence.py)
            w.note_marks_detected(s.marks())
        notes = [] if final else identify_download(w, s, path) + w.tidy_on_open(len(s.tiles))
        draw = f"/draw/{uuid.uuid4().hex[:10]}"
        with lock:
            unmount(mine.draw)
            app.mount(draw, create_app(w.video, sites, setup=s))
            mine.wizard, mine.setup, mine.draw = w, s, draw + "/"
        return {**public(), "notes": notes}

    def _channels_in(path: Path) -> int:
        """How many cameras this footage holds, or 0 when nothing says.

        Read from what was kept when it was downloaded: a local file, no network. A name alone
        does not say - "Export - Multiple Channels - ..." is any number of them - so only a
        remembered list of channels counts.
        """
        info = rn.download_info(path) or {}
        return len([c for c in (info.get("cameras") or []) if str(c).strip()])

    def identify_download(w: Wizard, s: Setup, path: Path) -> list[str]:
        """Footage downloaded from RetailNext: its brand, store and cameras are known, so the
        wizard uses them, and only that store's drawings are offered."""
        info = rn.download_info(path)
        if info is None:
            return []
        if not info.get("subscription"):  # an earlier download: check the name's store code
            try:
                conn, nodes, store = rn_store(str(info["code"]))
            except (HTTPException, rn.RetailNextError):
                return []  # not one of ours, or RetailNext is out of reach
            info = {**info, **rn.store_summary(conn.subscription, nodes, store)}
        s.store = (str(info["code"]), str(info.get("name") or ""))
        s.names = [str(n) for n in info.get("cameras") or []]  # the channels, in picture order
        return w.use_download(info)

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return public()

    glob.rn_nodes = {}

    def rn_connections() -> dict[str, rn.Connection]:
        conns = {c.subscription: c for c in rn.connections()}
        if not conns:
            raise HTTPException(400, "Not connected to RetailNext yet: run 'retailnext.py "
                                     "connect' once for each subscription (in the Windows app: "
                                     "CrossingCount retailnext connect).")
        return conns

    def rn_locations(conn: rn.Connection) -> list[dict[str, Any]]:
        nodes = glob.rn_nodes if glob.rn_nodes is not None else {}
        if conn.subscription not in nodes:
            nodes[conn.subscription] = rn.locations(conn)
        glob.rn_nodes = nodes
        return nodes[conn.subscription]

    def rn_store(code: str) -> tuple[rn.Connection, list[dict[str, Any]], dict[str, Any]]:
        """The store with this code, in whichever connected subscription has it."""
        conns = rn_connections()
        sets = {sub: rn_locations(c) for sub, c in conns.items()}
        sub, store = rn.find_store_in(sets, code)
        return conns[sub], sets[sub], store

    def rn_camera_count(code: str) -> int | None:
        """How many cameras RetailNext says this store has, or None when it cannot be asked.

        A check on the pictures found that has nothing to do with the pictures: no wall,
        line or shadow can fool it. It is a ceiling, not an answer - an export can hold
        fewer cameras than the store has, because whoever exported it chose the channels.
        """
        if not code:
            return None
        try:
            _, nodes, store = rn_store(code)
            return len(rn.video_channels(nodes, str(store["uuid"]))) or None
        except (HTTPException, rn.RetailNextError, OSError, KeyError):
            return None  # not connected, no such store, no network: the page still works

    def rn_day(text: str) -> date:
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise HTTPException(400, f"{text!r} is not a day (YYYY-MM-DD).") from exc

    def rn_lists() -> dict[str, Any]:
        """The brands: every usable one, this computer's own, and those built into the app."""
        return {"subscriptions": rn.subscriptions(), "saved": rn.saved_subscriptions(),
                "builtin": rn.builtin_subscriptions()}

    @app.get("/api/retailnext")
    def retailnext_status() -> dict[str, Any]:
        return {"connected": bool(rn.subscriptions()), **rn_lists(),
                "can_share": releases.can_share()}

    @app.post("/api/retailnext/connect")
    def retailnext_connect(c: ConnectIn) -> dict[str, Any]:
        """A brand not connected yet: its key, typed on the page, tried and then kept in this
        computer's credential store. Only the brand's name comes back."""
        try:
            conn = rn.connect(c.subscription, c.access_key, c.secret_key)
        except rn.RetailNextError as exc:
            raise HTTPException(400, str(exc)) from exc
        if glob.rn_nodes is not None:
            glob.rn_nodes.pop(conn.subscription, None)  # a new key may see other stores
        return {"subscription": conn.subscription, **rn_lists()}

    @app.post("/api/retailnext/reconnect")
    def retailnext_reconnect(f: ForgetIn) -> dict[str, Any]:
        """A brand whose key is already in this computer's credential store (saved before,
        or by the other copy of CrossingCount): connected with nothing to type."""
        try:
            conn = rn.reconnect(f.subscription)
        except rn.RetailNextError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"connected": conn is not None, **rn_lists()}

    @app.post("/api/retailnext/forget")
    def retailnext_forget(f: ForgetIn) -> dict[str, Any]:
        """Remove a brand's key from this computer."""
        try:
            sub = rn.subscription_name(f.subscription)
        except rn.RetailNextError as exc:
            raise HTTPException(400, str(exc)) from exc
        if sub in rn.builtin_subscriptions() and sub not in rn.saved_subscriptions():
            raise HTTPException(400, f"{sub} is built into this app, so it cannot be removed "
                                     f"here.")
        rn.forget_connection(sub)
        if glob.rn_nodes is not None:
            glob.rn_nodes.pop(sub, None)
        return rn_lists()

    @app.post("/api/share/brands")
    def share_brands() -> dict[str, Any]:
        """This computer's brands into GitHub's secrets, for the next builds of the apps."""
        saved = set(rn.saved_subscriptions())
        try:
            return releases.share_brands([c for c in rn.connections() if c.subscription in saved])
        except releases.ReleaseError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/share/token")
    def share_token(t: TokenIn) -> dict[str, Any]:
        """A token for the apps' update notice, into GitHub's secrets."""
        try:
            return releases.share_update_token(t.token)
        except releases.ReleaseError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/update")
    def update_status(fetch: bool = True) -> dict[str, Any]:
        """A newer version: on GitHub, or (in the project folder) on this computer and not
        yet running. Without fetch, only which version is running."""
        if paths.FROZEN:
            return releases.status() if fetch else releases.local_status()
        return updates.status(fetch=fetch)

    @app.post("/api/update")
    def update_apply() -> dict[str, Any]:
        """Take the newer version and start again on it. Not while work is running."""
        with lock:
            everyone = list(sessions.values())
        if any(p.wizard is not None and p.wizard.job_status()["status"] == "running"
               for p in everyone):
            raise HTTPException(409, "A count is running: let it finish (or stop it) first.")
        if any(p.rn_job and p.rn_job.get("state") in ("exporting", "downloading")
               for p in everyone):
            raise HTTPException(409, "Footage is downloading: let it finish first.")
        if paths.FROZEN:  # download and install in the background; the page follows the job
            if not (glob.update_job and glob.update_job.get("state") in
                    ("downloading", "installing", "restarting")):
                job: dict[str, Any] = {"state": "downloading", "done": 0, "total": None,
                                       "message": ""}
                glob.update_job = job
                threading.Thread(target=releases.run_update,
                                 args=(job, lambda: threading.Timer(1.5, _stop_server).start()),
                                 daemon=True).start()
            return {"ok": True, "job": True}
        try:
            now = updates.apply()
        except updates.UpdateError as exc:
            raise HTTPException(400, str(exc)) from exc
        threading.Timer(0.5, _restart).start()
        return {"ok": True, "head": now}

    @app.get("/api/update/job")
    def update_job() -> dict[str, Any]:
        return dict(glob.update_job or {})

    @app.post("/api/update/token")
    def update_token(t: TokenIn) -> dict[str, Any]:
        """The installed app's GitHub token, typed on the page: tried, then kept."""
        try:
            releases.connect(t.token)
        except releases.ReleaseError as exc:
            raise HTTPException(400, str(exc)) from exc
        return releases.status()

    @app.post("/api/quit")
    def quit_app() -> dict[str, Any]:
        """Close CrossingCount from the page (after answering, so the page hears back)."""
        threading.Timer(0.5, _stop_server).start()
        return {"ok": True}

    @app.get("/api/retailnext/stores")
    def retailnext_stores(subscription: str | None = None) -> dict[str, Any]:
        """One connected subscription's stores (or every one's), each with its subscription."""
        conns = rn_connections()
        if subscription:
            if subscription not in conns:
                raise HTTPException(400, f"{subscription} is not connected on this computer: run "
                                         f"'retailnext.py connect {subscription}'.")
            conns = {subscription: conns[subscription]}
        stores = []
        for sub, conn in conns.items():
            try:
                nodes = rn_locations(conn)
            except rn.RetailNextError as exc:
                raise HTTPException(400, f"{sub}: {exc}") from exc
            stores += [{"code": str(n["store_id"]), "name": str(n.get("name") or ""),
                        "subscription": sub} for n in nodes
                       if n.get("location_type") == "store" and n.get("store_id")
                       and not n.get("archive_date")]
        return {"stores": sorted(stores, key=lambda s: (s["code"], s["subscription"]))}

    @app.post("/api/retailnext/busiest")
    def retailnext_busiest(b: BusiestIn) -> dict[str, Any]:
        """The windows of the day to validate, by the sampling rule chosen (sampling.py), from
        RetailNext's traffic: by default the busiest for the traffic validated and a control
        window of lower traffic. Remembered, so a download records which window it is."""
        day = rn_day(b.date)
        try:
            conn, nodes, store = rn_store(b.code)
            uuid_ = str(store["uuid"])
            rows = rn.day_traffic(conn, uuid_, day, store.get("time_zone"))
            cameras = len(rn.video_channels(nodes, uuid_)) or len(rn.entrances(nodes, uuid_)) or 1
            plan = sampling.plan(rn.windows(rows, b.minutes, b.direction), mode=b.mode,
                                 length_min=b.minutes, direction=b.direction, cameras=cameras,
                                 seed=b.seed, day=day.isoformat())
        except (rn.RetailNextError, sampling.SamplingError) as exc:
            raise HTTPException(400, str(exc)) from exc
        me().rn_plan = {**plan, "request": [_bare(b.code), b.date]}
        return {"store": store.get("name"), "code": store.get("store_id"),
                "subscription": conn.subscription, "time_zone": store.get("time_zone"),
                "windows": plan["chosen"], "considered": len(plan["candidates"]),
                "sampling": {k: v for k, v in plan.items() if k not in ("candidates", "chosen")}}

    def _fetch(item: dict[str, Any]) -> None:
        """Export one window from RetailNext and download it. Raises RetailNextError or OSError.

        Everything the window needs is worked out here rather than when it was queued, because
        a queue can sit for an hour: the store's cameras, its clock and where the file goes are
        all read at the moment the work is done.
        """
        day = date.fromisoformat(item["date"])
        conn, nodes, store = rn_store(item["code"])
        tz = store.get("time_zone")
        if not tz:
            raise rn.RetailNextError(f"RetailNext gives no time zone for {store.get('name')}.")
        channels = rn.video_channels(nodes, str(store["uuid"]))
        if not channels:
            raise rn.RetailNextError(
                f"RetailNext has no camera video for {store.get('name')}: its counts come from "
                f"a sensor without video, so the footage has to come from elsewhere.")
        start_at, end_at = rn.local_period(day, item["start"], item["until"], str(tz))
        target = next((f for f in (folders or default_folders()) if f.is_dir()),
                      paths.data_root() / "footage")
        target.mkdir(parents=True, exist_ok=True)
        # one export, or the same window twice: the clean one to count on and to count by hand,
        # the marked one for RetailNext's own line and for watching its tracker (a pair)
        wanted = [False, True] if item["both"] else [item["marks"]]
        dests = [rn.free_path(target, rn.footage_name(str(store.get("store_id") or item["code"]),
                                                      start_at, end_at, m)) for m in wanted]
        item.update(cameras=sorted(channels), of=len(wanted),
                    store=store.get("name") or item.get("store"))
        for which, (marks, where) in enumerate(zip(wanted, dests, strict=True), start=1):
            say = "with RetailNext's marks" if marks else "clean"
            item.update(which=which, state="exporting", done=0, total=None,
                        message=f"RetailNext is preparing the video ({say})…")
            export_id = rn.start_export(conn, list(channels.values()), start_at, end_at, marks)
            waited = 0.0
            while True:
                state, info = rn.export_status(conn, export_id)
                if state == "ready":
                    break
                if state == "failed":
                    raise rn.RetailNextError(f"RetailNext could not export the video: {info}")
                if state == "expired":
                    raise rn.RetailNextError("RetailNext no longer has this export: try again.")
                if waited >= EXPORT_WAIT_S:
                    raise rn.RetailNextError("RetailNext has not finished the export after "
                                             "30 minutes: try again later.")
                time.sleep(EXPORT_POLL_S)
                waited += EXPORT_POLL_S
                item["message"] = f"RetailNext is preparing the video ({say})… ({int(waited)} s)"
            item.update(state="downloading", message=f"Downloading ({say})…")
            rn.download(info, where, lambda done, total: item.update(done=done, total=total))
        for marks, where in zip(wanted, dests, strict=True):
            twin = next((str(o.name) for m, o in zip(wanted, dests, strict=True)
                         if m is not marks), None)
            rn.remember_download(where, {**rn.store_summary(conn.subscription, nodes, store),
                                         "marks": marks, "start": start_at.isoformat(),
                                         "end": end_at.isoformat(), "sampling": item.get("sampling"),
                                         "twin": twin})
        item.update(state="ready", message="Downloaded.",
                    path=str(dests[-1 if item["marks"] else 0]),
                    paths=[str(p) for p in dests])

    def _drain(mine: _Current) -> None:
        """Work through one person's queue in order, one window at a time.

        One at a time because RetailNext prepares an export before it can be downloaded, and
        asking for several at once does not make it faster. A window that fails is left in the
        queue marked failed and the next one starts: one store being unavailable must not stop
        the others.
        """
        while True:
            item = next((i for i in mine.rn_queue if i["state"] == "waiting"), None)
            if item is None:
                mine.rn_working = False
                return
            mine.rn_job = item
            try:
                _fetch(item)
            except rn.RetailNextError as exc:
                item.update(state="failed", message=str(exc))
            except OSError as exc:
                item.update(state="failed", message=f"Could not save the video: {exc}")
            except Exception as exc:  # noqa: BLE001  a queue must never die on one window
                item.update(state="failed", message=f"{type(exc).__name__}: {exc}")

    def _start(mine: _Current) -> None:
        if not mine.rn_working:
            mine.rn_working = True
            threading.Thread(target=_drain, args=(mine,), daemon=True).start()

    def _counted(path: str | None) -> bool:
        """Whether this footage already has a finished validation.

        A downloaded window stays "ready" for as long as the app is open, so without this the
        page offers the first of them again the moment the second is opened, and a person goes
        round in circles between two windows. Counted means a report was made, not merely
        opened: somebody who starts one and stops is still pointed back at it.
        """
        if not path:
            return False
        kept = ((runs_root or paths.data_root()) / default_run_dir(path, None)
                / "wizard" / "state.json")
        try:
            return bool(json.loads(kept.read_text(encoding="utf-8")).get("report"))
        except (OSError, ValueError):
            return False

    def _queued(mine: _Current) -> dict[str, Any]:
        items = [{**i, "counted": _counted(i.get("path"))} for i in mine.rn_queue]
        return {"items": items, "working": mine.rn_working,
                "ready": [i for i in items if i["state"] == "ready"]}

    def _item(code: str, store: str, day: str, window: dict[str, Any], b: Any,
              chosen_by: dict[str, Any] | None) -> dict[str, Any]:
        # a window's own words for its edges: retailnext.windows gives "start" and "until"
        frm, until = str(window["start"]), str(window["until"])
        return {"id": f"{code}-{day}-{frm}".replace(":", ""), "code": _bare(code),
                "store": store, "date": day, "start": frm, "until": until,
                "marks": bool(b.marks), "both": bool(b.both), "sampling": chosen_by,
                "count": getattr(b, "count", "auto"),
                "direction": getattr(b, "direction_for", "") or getattr(b, "direction", ""),
                "label": f"{code} {store} · {frm}–{until}"
                         + (f" · {window.get('role')}" if window.get("role") else ""),
                "state": "waiting", "message": "Waiting for the ones before it.",
                "done": 0, "total": None, "which": 1, "of": 2 if b.both else 1,
                "path": None, "paths": [], "cameras": []}

    @app.post("/api/retailnext/queue")
    def retailnext_queue(b: QueueIn) -> dict[str, Any]:
        """Queue several stores' footage: each store's windows by the sampling rule, the
        stores in the order they were given, and one window fetched at a time."""
        mine = me()
        day = rn_day(b.date)
        added: list[dict[str, Any]] = []
        problems: list[str] = []
        for code in b.codes:
            try:
                conn, nodes, store = rn_store(code)
                uuid_ = str(store["uuid"])
                rows = rn.day_traffic(conn, uuid_, day, store.get("time_zone"))
                cameras = len(rn.video_channels(nodes, uuid_)) or len(rn.entrances(nodes, uuid_)) or 1
                # the window is picked on the traffic being validated: entries for an in
                # validation, exits for an out one, the two added for a both. The page has to
                # send a single direction as well, because the wizard opens on one, so the
                # traffic actually validated comes through direction_for; scoring a both
                # validation on entries alone picks the wrong quarter of an hour.
                scoring = b.direction_for or b.direction
                plan = sampling.plan(rn.windows(rows, b.minutes, scoring), mode=b.mode,
                                     length_min=b.minutes, direction=scoring,
                                     cameras=cameras, seed=b.seed, day=day.isoformat())
            except (rn.RetailNextError, sampling.SamplingError) as exc:
                problems.append(f"{code}: {exc}")
                continue
            name = str(store.get("name") or "")
            # the busiest window first, then the rest, and only as many as were asked for:
            # queueing a store's whole plan fills the queue faster than anyone can count it.
            # By the traffic in the window, not by what the rule called it: under the
            # stratified rule every window chosen is a "stratum" of rank 1, and the strata are
            # built quiet upwards, so going by role and rank took the quietest window of the
            # day. Role and rank only settle a tie, which is where the peak rule puts its
            # busiest anyway.
            busiest = sorted(plan["chosen"],
                             key=lambda w: (-float(w.get("score") or 0),
                                            w.get("role") != "peak", w.get("rank") or 99))
            for w in busiest[:max(1, b.per_store)]:
                chosen_by = sampling.record(plan, w["start"], w["until"])
                item = _item(str(store.get("store_id") or code), name, day.isoformat(), w, b,
                             chosen_by)
                if not any(i["id"] == item["id"] for i in mine.rn_queue):
                    mine.rn_queue.append(item)
                    added.append(item)
        if not added and problems:
            raise HTTPException(400, " ".join(problems))
        _start(mine)
        return {**_queued(mine), "added": len(added), "problems": problems}

    @app.get("/api/retailnext/queue")
    def retailnext_queue_state() -> dict[str, Any]:
        return _queued(me())

    @app.post("/api/retailnext/queue/forget")
    def retailnext_queue_forget(r: RunRef) -> dict[str, Any]:
        """Take one out of the queue. Footage already downloaded stays on the disk."""
        mine = me()
        mine.rn_queue = [i for i in mine.rn_queue
                         if i["id"] != r.id and not (r.id == "done" and i["state"] == "ready")]
        return _queued(mine)

    @app.post("/api/retailnext/download")
    def retailnext_download(b: DownloadIn) -> dict[str, Any]:
        """One window, which is a queue of one: the same worker fetches it."""
        mine = me()
        if any(i["state"] in ("waiting", "exporting", "downloading") for i in mine.rn_queue):
            raise HTTPException(409, "A download is already under way.")
        plan = (mine.rn_plan if mine.rn_plan and mine.rn_plan["request"] == [_bare(b.code), b.date]
                else None)
        item = _item(b.code, "", b.date, {"start": b.start, "until": b.until}, b,
                     sampling.record(plan, b.start, b.until) if plan else None)
        mine.rn_queue = [i for i in mine.rn_queue if i["id"] != item["id"]]
        mine.rn_queue.append(item)
        _start(mine)
        return item

    @app.get("/api/retailnext/download")
    def retailnext_download_status() -> dict[str, Any]:
        job = me().rn_job
        return {**job, "state": "done"} if job and job["state"] == "ready" else (
            job or {"state": "idle"})

    @app.post("/api/retailnext/fetch")
    def retailnext_fetch() -> dict[str, Any]:
        """RetailNext's own numbers for these cameras and this period, from its API."""
        w = wiz()
        start, end = w.period()
        if start is None or end is None:
            raise HTTPException(400, "The video's name has no clock time, so RetailNext's "
                                     "numbers cannot be looked up.")
        code = w.state["store"]["code"]
        sub = (w.state.get("retailnext") or {}).get("subscription")  # the footage's own brand
        try:
            conn, nodes, store = rn_store(f"{sub}/{code}" if sub else code)
            adapter = sensors.RetailNextAdapter(conn, nodes, str(store.get("store_id") or code))
            data = adapter.fetch([c["sensor"] for c in w.state["cameras"]], start, end)
        except rn.RetailNextError as exc:
            raise HTTPException(400, str(exc)) from exc
        warnings: list[str] = []
        return {**run(lambda: warnings.extend(w.use_sensor(data))),
                "retailnext_warnings": warnings}

    @app.post("/api/sensor/csv")
    def sensor_csv(c: CsvIn) -> dict[str, Any]:
        """Another counting system's numbers, from a CSV file (docs/SENSOR_DATA.md)."""
        w = wiz()
        start, end = w.period()
        if start is None or end is None:
            raise HTTPException(400, "The video's name has no clock time, so the file's numbers "
                                     "cannot be lined up with it.")
        if len(c.text) > 20_000_000:
            raise HTTPException(400, "The file is too large (more than 20 MB).")
        try:
            data = sensors.CsvAdapter(c.text, Path(c.name).name or "numbers.csv").fetch(
                [cam["sensor"] for cam in w.state["cameras"]], start.replace(tzinfo=None),
                end.replace(tzinfo=None))
        except sensors.SensorError as exc:
            raise HTTPException(400, str(exc)) from exc
        warnings: list[str] = []
        return {**run(lambda: warnings.extend(w.use_sensor(data))), "sensor_warnings": warnings}

    @app.get("/api/drawn")
    def drawn() -> dict[str, Any]:
        s = setup()
        cfgs = s.existing()
        marks = s.marks()
        w = wiz()
        info = w.state.get("retailnext") or {}
        code = str(info.get("code") or w.state["store"].get("code")
                   or (rn.download_info(w.video) or {}).get("code") or "")
        have = rn_camera_count(code)
        many = f"More pictures than cameras: {len(s.tiles)} were found in this video, but " \
               f"RetailNext says {code} has {have}. One camera's picture may have been cut " \
               f"in two. Count only the pictures that are whole cameras." \
            if have is not None and len(s.tiles) > have else None
        return {"store_cameras": have, "too_many": many, "pictures": [
            {"picture": t.index, "marks": bool(marks[t.index]["marks"]),
             "configs": [{"sensor": c["sensor"], "file": c["file"], "how": c["how"],
                          "confident": c["confident"], "alignment": c["alignment"],
                          "correspondence": c["correspondence"]} for c in cfgs
                         if c["picture"] == t.index and not c["problem"]]}
            for t in s.tiles],
            # a clean download can be calibrated against a minute of the same window with marks
            "can_calibrate": bool(info.get("start") and info.get("subscription")
                                  and info.get("code") and not any(m["marks"] for m in marks)),
            # footage from RetailNext: its cameras' names, picture by picture
            "suggested": (wiz().state.get("retailnext") or {}).get("cameras", []),
            # RetailNext's blue lines found on the people-free picture, or a saved drawing
            # whose recorded blue line matches this video
            "marks_guess": any(bool(m["marks"]) for m in marks) or any(
                c["picture"] is not None and c["match"] is not None
                and c["overlay_hue"] is not None
                and ov.MARK_HUES[0] <= c["overlay_hue"] <= ov.MARK_HUES[1] for c in cfgs)}

    @app.post("/api/mode")
    def mode(m: ModeIn) -> dict[str, Any]:
        return run(lambda: wiz().set_mode(m.mode))

    @app.post("/api/marks")
    def marks(m: MarksIn) -> dict[str, Any]:
        return run(lambda: wiz().set_marks(m.marks))

    @app.post("/api/named-cameras")
    def named_cameras(n: NamedCamerasIn) -> dict[str, Any]:
        s = setup()
        return run(lambda: wiz().set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                                                   n.cameras))

    def hand() -> dict[str, Any]:
        return {**public(), "hand": wiz().manual_summary()}

    def hand_run(change: Callable[[], object]) -> dict[str, Any]:
        run(change)
        return hand()

    @app.get("/api/hand")
    def hand_state() -> dict[str, Any]:
        return {**hand(), "geometry": wiz().geometry()}

    @app.post("/api/hand/add")
    def hand_add(a: HandAddIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_add(a.camera, a.t, a.direction, a.uncertain, a.note))

    @app.post("/api/hand/delete")
    def hand_delete(i: HandIdIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_delete(i.id))

    @app.post("/api/hand/edit")
    def hand_edit(e: HandEditIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_edit(e.id, e.t, e.direction, e.uncertain, e.note,
                                                  e.reason))

    @app.post("/api/hand/undo")
    def hand_undo(c: HandCameraIn) -> dict[str, Any]:
        removed: list[dict[str, Any] | None] = []
        out = hand_run(lambda: removed.append(wiz().manual_undo(c.camera)))
        return {**out, "removed": removed[0] if removed else None}

    @app.post("/api/hand/watched")
    def hand_watched(w: HandWatchedIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_watched(w.camera, w.start, w.end))

    @app.post("/api/hand/position")
    def hand_position(p: HandPositionIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_position(p.camera, p.t))

    @app.post("/api/hand/done")
    def hand_done(d: DoneIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_done(d.done))

    # ---- counting the total only ---------------------------------------------------------
    # Its own routes, on purpose: a total is not a crossing, and nothing here reaches the
    # crossings, the gold set or the scoring.

    def total() -> dict[str, Any]:
        return {**public(), "total": wiz().total_summary()}

    def total_run(change: Callable[[], object]) -> dict[str, Any]:
        run(change)
        return total()

    @app.get("/api/total")
    def total_state() -> dict[str, Any]:
        return total()

    @app.post("/api/total/step")
    def total_step(t: TotalStepIn) -> dict[str, Any]:
        return total_run(lambda: wiz().total_step(t.camera, t.direction, t.step))

    @app.post("/api/total/set")
    def total_set(t: TotalSetIn) -> dict[str, Any]:
        return total_run(lambda: wiz().total_set(t.camera, t.direction, t.value))

    @app.post("/api/total/notes")
    def total_notes(t: TotalNotesIn) -> dict[str, Any]:
        return total_run(lambda: wiz().total_notes(t.notes))

    @app.post("/api/total/done")
    def total_done(t: TotalDoneIn) -> dict[str, Any]:
        return total_run(lambda: wiz().total_done(t.done, t.whole_clip))

    @app.get("/api/network")
    def network_status() -> dict[str, Any]:
        """Sharing on this network: the address and code (on this computer only), and who is
        using it now."""
        if remote():
            return {"on_host": False, "on": True}
        with lock:
            people = [{"who": p.who(), "footage": p.wizard.video.name if p.wizard else None,
                       "counting": bool(p.wizard and p.wizard.job_status()["status"] == "running"),
                       "idle_s": round(time.time() - p.last_seen)}
                      for p in sessions.values() if p.address is not None]
        return {"on_host": True, **sharing.status(), "people": people}

    @app.post("/api/network")
    def network_switch(n: NetworkIn) -> dict[str, Any]:
        """Let people on this network use CrossingCount, or stop. Stopping keeps their work
        (saved as it went) and lets a count already running finish."""
        if n.on and not sharing.start():
            raise HTTPException(400, sharing.error or "Could not start sharing.")
        if not n.on:
            sharing.stop()
            with lock:
                for sid in [k for k, p in sessions.items() if p.address is not None]:
                    person = sessions[sid]
                    if not (person.wizard and person.wizard.job_status()["status"] == "running"):
                        drop(person)
                        del sessions[sid]
        paths.save_settings({"network": n.on})
        return network_status()

    @app.get("/api/tool-vs-hand")
    def tool_vs_hand() -> dict[str, Any]:
        """After a hand count, with the tool counting the clean twin: the two side by side."""
        return {"comparison": wiz().tool_vs_hand()}

    @app.post("/api/diagnosis")
    def diagnosis(d: DiagnosisIn) -> dict[str, Any]:
        """Why the two differ here, watching RetailNext's own tracker: kept with the count."""
        run(lambda: wiz().set_diagnosis(d.key, d.cause, d.note))
        return {"comparison": wiz().tool_vs_hand()}

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        kept = paths.load_settings()
        return {"examples_dir": kept.get("examples_dir", ""),
                "delivery_dir": kept.get("delivery_dir", "")}

    @app.post("/api/settings")
    def set_settings(s: SettingsIn) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, value in (("examples_dir", s.examples_dir),
                            ("delivery_dir", s.delivery_dir)):
            if value is None:
                continue
            if not value.strip():
                out[name] = paths.save_settings({name: ""})[name]
                continue
            try:
                folder = check_folder(value.strip())
            except (OSError, ValueError) as exc:
                raise HTTPException(400, f"Can't use that folder: {exc}") from exc
            out[name] = paths.save_settings({name: str(folder)})[name]
        return {**get_settings(), **out}

    def delivery_root() -> Path | None:
        where = paths.load_settings().get("delivery_dir")
        return Path(where) if where else None

    @app.get("/api/deliver")
    def delivery_says(footage: bool = False) -> dict[str, Any]:
        """Where this validation would go and what would go with it: nothing is copied."""
        return {"plan": wiz().delivery_plan(delivery_root(), footage),
                "delivered": wiz().state.get("delivered")}

    @app.post("/api/deliver")
    def deliver(d: DeliverIn) -> dict[str, Any]:
        root = delivery_root()
        if root is None:
            raise HTTPException(400, "Set the folder for finished validations first "
                                     "(Report page).")
        w = wiz()
        try:
            got = w.deliver(root, d.footage)
        except WizardError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {**got, "delivered": w.state.get("delivered")}

    # ---------- the Microsoft account the app writes to SharePoint with ----------
    # Only the refresh token is a secret and it never leaves the credential store; the tenant
    # and client id are not, and are kept beside the folders. Signing in and out is this
    # computer's to do, like the RetailNext keys.
    signing_in: dict[str, Any] = {}

    @app.get("/api/microsoft")
    def microsoft() -> dict[str, Any]:
        kept = paths.load_settings()
        out: dict[str, Any] = {
            "signed_in": graph.signed_in(),
            "tenant": kept.get("graph_tenant", ""), "client_id": kept.get("graph_client_id", ""),
            "host": kept.get("graph_host", ""), "registered": True}
        try:
            graph.registration()
        except graph.NotSignedIn as exc:
            out |= {"registered": False, "problem": str(exc)}
        if started := signing_in.get("started"):
            out["code"] = {"code": started.code, "url": started.url,
                           "expires_in": max(0, int(started.expires_at - time.time()))}
        return out

    @app.post("/api/microsoft")
    def microsoft_set(d: MicrosoftIn) -> dict[str, Any]:
        paths.save_settings({"graph_tenant": d.tenant.strip(),
                             "graph_client_id": d.client_id.strip(),
                             "graph_host": d.host.strip()})
        return microsoft()

    @app.post("/api/microsoft/signin")
    def microsoft_signin() -> dict[str, Any]:
        """Start a sign-in, or ask once whether the code has been entered yet."""
        try:
            started = signing_in.get("started")
            if started is None or started.expires_at < time.time():
                signing_in["started"] = graph.begin()
                return microsoft()
            if graph.poll(started):
                signing_in.pop("started", None)
        except graph.GraphError as exc:
            signing_in.pop("started", None)
            raise HTTPException(400, str(exc)) from exc
        return microsoft()

    @app.post("/api/microsoft/forget")
    def microsoft_forget() -> dict[str, Any]:
        signing_in.pop("started", None)
        graph.forget()
        return microsoft()

    @app.get("/api/examples")
    def examples() -> dict[str, Any]:
        return {"examples": wiz().state.get("examples")}

    @app.post("/api/cameras")
    def cameras() -> dict[str, Any]:
        s = setup()
        return run(lambda: wiz().set_cameras(s.existing(), [t.as_dict() for t in s.tiles]))

    @app.post("/api/cameras/confirm")
    def cameras_confirm(p: PictureIn) -> dict[str, Any]:
        """A person says this picture really is that camera (a match the tool was unsure of)."""
        return run(lambda: wiz().confirm_camera(p.picture))

    @app.post("/api/calibrate")
    def calibrate() -> dict[str, Any]:
        """Export a minute of this footage again with RetailNext's marks and open the drawing
        page on it: a line drawn there sits on RetailNext's own line, and is then used on this
        clean footage (correspondence.py; Ground Truth Specification section 10)."""
        from datetime import datetime as dt
        from datetime import timedelta as td
        from zoneinfo import ZoneInfo

        w, s, mine = wiz(), setup(), me()
        info = dict(w.state.get("retailnext") or {})
        if not (info.get("start") and info.get("end") and info.get("subscription")
                and info.get("code")):
            raise HTTPException(400, "Calibrating needs footage downloaded from RetailNext here: "
                                     "its store and period have to be known.")
        if mine.rn_job and mine.rn_job.get("state") in ("exporting", "downloading"):
            raise HTTPException(409, "A download is already under way.")
        try:
            conn, nodes, store = rn_store(f"{info['subscription']}/{info['code']}")
            channels = rn.video_channels(nodes, str(store["uuid"]))
            if not channels:
                raise rn.RetailNextError(f"RetailNext has no camera video for "
                                         f"{store.get('name')}.")
            # the store's own clock, named as RetailNext names it (AEST), as the footage was
            zone = ZoneInfo(str(info.get("time_zone") or store.get("time_zone") or "UTC"))
            start = dt.fromisoformat(str(info["start"])).astimezone(zone)
            end = min(start + td(seconds=CALIBRATE_S),
                      dt.fromisoformat(str(info["end"])).astimezone(zone))
        except (rn.RetailNextError, ValueError, KeyError) as exc:  # KeyError: unknown time zone
            raise HTTPException(400, str(exc)) from exc
        target = paths.data_root() / "calibration"
        target.mkdir(parents=True, exist_ok=True)
        dest = rn.free_path(target, rn.footage_name(str(info["code"]), start, end, True))
        job: dict[str, Any] = {
            "state": "exporting", "done": 0, "total": None, "path": None, "draw_url": None,
            "message": "RetailNext is preparing a minute of it with its marks…"}
        mine.rn_job = job

        def work() -> None:
            try:
                export_id = rn.start_export(conn, list(channels.values()), start, end, True)
                waited, link = 0.0, ""
                while True:
                    state, got = rn.export_status(conn, export_id)
                    if state == "ready":
                        link = got
                        break
                    if state in ("failed", "expired"):
                        raise rn.RetailNextError(f"RetailNext could not export it: {got or state}")
                    if waited >= EXPORT_WAIT_S:
                        raise rn.RetailNextError("RetailNext has not finished the export.")
                    time.sleep(EXPORT_POLL_S)
                    waited += EXPORT_POLL_S
                job.update(state="downloading", message="Downloading…")
                rn.download(link, dest, lambda done, total: job.update(done=done, total=total))
                rn.remember_download(dest, {**rn.store_summary(conn.subscription, nodes, store),
                                            "marks": True, "calibration": True,
                                            "start": start.isoformat(), "end": end.isoformat()})
                cal = Setup(dest, sites)
                cal.store, cal.names = s.store, s.names
                where = f"/calibrate/{uuid.uuid4().hex[:10]}"
                with lock:
                    unmount(mine.calibrate)
                    app.mount(where, create_app(dest, sites, setup=cal))
                    mine.calibrate = where + "/"
                job.update(state="done", message="Ready to draw.", path=str(dest),
                           draw_url=where + "/")
            except (rn.RetailNextError, FFmpegError, OSError, ValueError) as exc:
                job.update(state="failed", message=str(exc))

        threading.Thread(target=work, daemon=True).start()
        return job

    @app.get("/api/calibrate")
    def calibrate_status() -> dict[str, Any]:
        return me().rn_job or {"state": "idle"}

    @app.post("/api/audit/marks")
    def audit_marks() -> dict[str, Any]:
        """Earlier results whose footage shows RetailNext's marks although they say clean."""
        return {"findings": independence.audit(data())}

    @app.post("/api/audit/marks/reclassify")
    def audit_marks_reclassify() -> dict[str, Any]:
        """Keep those results out of every comparison with the system, with the reason."""
        found = independence.audit(data())
        independence.reclassify(data(), found, operator_name())
        return {"reclassified": found}

    @app.post("/api/direction")
    def direction(d: DirectionIn) -> dict[str, Any]:
        return run(lambda: wiz().set_direction(d.direction))

    @app.post("/api/rules")
    def rules(r: RulesIn) -> dict[str, Any]:
        return run(lambda: wiz().set_rules(r.children, r.staff))

    @app.post("/api/sensor")
    def sensor(s: SensorIn) -> dict[str, Any]:
        return run(lambda: wiz().set_sensor(s.values, s.intervals, s.cameras))

    @app.post("/api/model")
    def model(m: ModelIn) -> dict[str, Any]:
        return run(lambda: wiz().set_model(m.model))

    @app.post("/api/run")
    def start(r: RunIn | None = None) -> dict[str, Any]:
        return run(lambda: wiz().start(confirm=bool(r and r.confirm)))

    @app.post("/api/stop")
    def stop() -> dict[str, Any]:
        return run(wiz().stop)

    @app.get("/api/job")
    def job() -> dict[str, Any]:
        return wiz().job_status()

    @app.get("/api/preview.jpg")
    def preview(picture: int, t: float = 0.0) -> Response:
        try:
            data = wiz().preview_jpeg(picture, t)
        except WizardError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/check")
    def check() -> dict[str, Any]:
        w = wiz()
        return {"items": w.check_items(), "watch": w.watch_ranges(), "cameras": w.geometry(),
                "answers": w.state["answers"], "added": w.state["added"],
                "watched": w.state["watched"], "watch_mode": w.state["watch"],
                "counts": w.counts()}

    @app.post("/api/answer")
    def answer(a: AnswerIn) -> dict[str, Any]:
        return run(lambda: wiz().answer(a.id, a.answer, a.people))

    @app.post("/api/add")
    def add(a: AddIn) -> dict[str, Any]:
        return run(lambda: wiz().add(a.camera, a.t, a.direction, a.range))

    @app.post("/api/remove-added")
    def remove_added(i: IndexIn) -> dict[str, Any]:
        return run(lambda: wiz().remove_added(i.index))

    @app.post("/api/watch")
    def watch(m: WatchIn) -> dict[str, Any]:
        return run(lambda: wiz().set_watch(m.mode))

    @app.post("/api/watched")
    def watched(r: WatchedIn) -> dict[str, Any]:
        return run(lambda: wiz().mark_watched(r.range, r.done))

    @app.post("/api/store")
    def store(s: StoreIn) -> dict[str, Any]:
        if s.operator and s.operator.strip():  # the checker's name, for the next video too
            if remote():  # someone on the network: theirs, not this computer's setting
                me().operator = s.operator.strip()
            else:
                paths.save_settings({"operator": s.operator.strip()})
        return run(lambda: wiz().set_store(s.name, s.code, s.location, s.report_date,
                                           s.operator))

    @app.post("/api/report")
    def report() -> dict[str, Any]:
        w = wiz()
        try:
            path = w.make_report(logo)
        except WizardError as exc:
            raise HTTPException(400, str(exc)) from exc
        folder = paths.load_settings().get("examples_dir")
        if folder:  # learning examples go to the shared folder in the background
            try:
                w.save_examples(check_folder(folder))
            except (OSError, ValueError) as exc:
                w.state["examples"] = {"status": "failed", "error": f"{folder}: {exc}"}
        return {"path": str(path), "name": path.name, **public()}

    @app.get("/api/report.pptx")
    def report_file() -> FileResponse:
        made = wiz().state.get("report")
        if not made or not Path(made["path"]).is_file():
            raise HTTPException(404, "No report yet.")
        path = Path(made["path"])
        return FileResponse(path, media_type=PPTX, filename=path.name)

    @app.get("/api/report.pdf")
    def report_pdf() -> FileResponse:
        made = wiz().state.get("report")
        if not made or not made.get("pdf") or not Path(made["pdf"]).is_file():
            raise HTTPException(404, "No PDF report yet: make the report again.")
        path = Path(made["pdf"])
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    @app.post("/api/reveal")
    def reveal() -> dict[str, bool]:
        made = wiz().state.get("report")
        if made and sys.platform == "darwin":
            subprocess.run(["/usr/bin/open", "-R", made["path"]], check=False)
        elif made and sys.platform == "win32":
            subprocess.run(["explorer", f"/select,{made['path']}"], check=False)
        return {"ok": bool(made)}

    @app.get("/video")
    def video_file(request: Request) -> Response:
        return video_range(wiz().video, request.headers.get("range"))

    # ---- the gold set ----------------------------------------------------------------------

    def data() -> Path:
        return runs_root if runs_root is not None else paths.data_root()

    @app.get("/gold", response_class=HTMLResponse)
    @app.get("/gold/", response_class=HTMLResponse)
    def gold_page() -> str:
        return (WEB_DIR / "gold.html").read_text(encoding="utf-8")

    def shared() -> Path | None:
        """The team's shared folder (the report page's shared folder), when set and there."""
        where = paths.load_settings().get("examples_dir")
        p = Path(str(where)).expanduser() if where else None
        return p if p is not None and p.is_dir() else None

    @app.get("/api/gold")
    def gold_overview() -> dict[str, Any]:
        return gold.overview(data(), shared())

    @app.get("/sensors", response_class=HTMLResponse)
    @app.get("/sensors/", response_class=HTMLResponse)
    def sensors_page() -> str:
        return (WEB_DIR / "sensors.html").read_text(encoding="utf-8")

    @app.get("/api/sensors")
    def sensors_overview(system: str | None = None) -> dict[str, Any]:
        """Every validation's result for one counting system, put together."""
        return validation.overview(data(), shared(), system)

    @app.post("/api/finalise")
    def finalise() -> dict[str, Any]:
        """Give this validation its ID and keep it, locked (runs.py)."""
        w = wiz()
        try:
            got = w.finalise(shared())
        except WizardError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {**public(), "run": got}

    @app.post("/api/new-version")
    def new_version() -> dict[str, Any]:
        return run(wiz().new_version)

    @app.get("/runs", response_class=HTMLResponse)
    @app.get("/runs/", response_class=HTMLResponse)
    def runs_page() -> str:
        return (WEB_DIR / "runs.html").read_text(encoding="utf-8")

    @app.get("/api/runs")
    def runs_list() -> dict[str, Any]:
        """Every finalised validation, checked against its manifest; and the audit log."""
        root = data()
        found = runs.listing(root)
        return {"runs": found, "coverage": runs.coverage(found),
                "audit": auditlog.verify(root), "recent": auditlog.recent(40, root)}

    @app.post("/api/runs/reveal")
    def runs_reveal(r: RunRef) -> dict[str, bool]:
        folder = runs.runs_dir(data()) / Path(r.id).name
        if not runs.ID_RE.match(folder.name) or not folder.is_dir():
            raise HTTPException(404, "There is no such validation on this computer.")
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/open", str(folder)], check=False)
        elif sys.platform == "win32":
            subprocess.run(["explorer", str(folder)], check=False)
        return {"ok": True}

    @app.post("/api/engagement")
    def engagement_summary(e: EngagementIn) -> dict[str, Any]:
        """Finalised windows of one store put together (engagement.py)."""
        try:
            return engagement.summarise(data(), e.ids)
        except engagement.EngagementError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/engagement/keep")
    def engagement_keep(e: EngagementIn) -> dict[str, Any]:
        """Keep the engagement under its own ID, read-only."""
        try:
            return engagement.write(data(), e.ids, operator_name())
        except engagement.EngagementError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/gold/current")
    def gold_current() -> dict[str, Any]:
        """Can this footage's count be kept as a gold clip, and is it kept already?"""
        w = wiz()
        rec = gold.find(w.state, data(), shared())
        tier = gold.footage_tier(w.state)
        split = gold.split_of(str(w.state["store"].get("code") or ""))
        return {"problems": gold.problems(w.state), "tier": tier,
                "tier_words": gold.tier_words(tier, split), "tags": gold.TAGS,
                "lighting": gold.LIGHTING, "occlusion": gold.OCCLUSION,
                "shared": shared() is not None, "clip": gold.describe(rec) if rec else None}

    @app.post("/api/gold/save")
    def gold_save(g: GoldSaveIn) -> dict[str, Any]:
        w = wiz()
        try:
            return {"clip": gold.save(w.state, w.run_dir, g.tags, g.notes, data(), g.lighting,
                                      g.occlusion, shared())}
        except gold.GoldError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/gold/adjudicate")
    def gold_adjudicate(a: AdjudicateIn) -> dict[str, Any]:
        """Settle moments two people's counts disagree on; both counts are kept."""
        w = wiz()
        try:
            return {"clip": gold.adjudicate(w.state, a.decisions, a.by, data(), shared())}
        except gold.GoldError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/gold/freeze")
    def gold_freeze(f: GoldFreezeIn) -> dict[str, Any]:
        """Freeze the set as it is: a version written once, never rewritten."""
        try:
            rel = gold.freeze(data(), f.note, shared())
        except gold.GoldError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {k: rel[k] for k in ("dataset_version", "created_at", "content_hash", "crossings")}

    @app.post("/api/gold/recount")
    def gold_recount() -> dict[str, Any]:
        return run(wiz().recount)

    @app.post("/api/gold/score")
    def gold_score(s: GoldScoreIn) -> dict[str, Any]:
        """Score the automatic count on gold clips, in the background (replays take a while)."""
        if s.which not in ("development", "test"):
            raise HTTPException(400, "Score the development set or the test set.")
        if glob.gold_job and glob.gold_job.get("state") == "running":
            raise HTTPException(409, "Already scoring: wait for it to finish.")
        job: dict[str, Any] = {"state": "running", "which": s.which}
        glob.gold_job = job

        def work() -> None:
            try:
                job.update(state="done", result=gold.evaluate(s.which, data(), s.note, shared(),
                                                              clean_only=s.clean_only))
            except (gold.GoldError, OSError, ValueError) as exc:
                job.update(state="failed", error=str(exc))

        threading.Thread(target=work, daemon=True).start()
        return dict(job)

    @app.get("/api/gold/score")
    def gold_score_status() -> dict[str, Any]:
        return dict(glob.gold_job or {})

    @app.get("/api/gold/experiment")
    def gold_experiment(id: str) -> dict[str, Any]:
        path = gold.experiments_dir(data()) / f"{Path(id).name}.json"
        if not path.is_file():
            raise HTTPException(404, "No such scoring.")
        import json

        return dict(json.loads(path.read_text(encoding="utf-8")))

    return app
