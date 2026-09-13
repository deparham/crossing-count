"""RetailNext's own counts, from its cloud API: optional, and downloads only.

The only requests are data queries to <subscription>.api.retailnext.net, made when a
person asks for RetailNext's numbers; no footage or result is ever sent. The access key
and secret key live in this computer's credential store (Keychain on a Mac, Credential
Manager on Windows) through keyring, never in a file, a report, a log or git;
settings.json keeps only the subscription name.

As documented (retailnext.atlassian.net, PUBLICDOCS, "API"): Basic authentication with
the access key and secret key; POST v2/location lists locations; POST v2/datamine gives
metrics such as traffic_in and traffic_out, grouped by time.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import keyring
import keyring.errors

from . import paths
from .util import write_json_atomic

SERVICE = "CrossingCount RetailNext"  # the credential store's entry
TIMEOUT_S = 30
RETRIES = 3
_SUBSCRIPTION = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_open = urllib.request.urlopen  # replaced in tests: nothing there reaches the network


class RetailNextError(Exception):
    """A request RetailNext refused or could not answer, said plainly."""


@dataclass(frozen=True)
class Connection:
    subscription: str
    access_key: str
    secret_key: str

    @property
    def base(self) -> str:
        return f"https://{self.subscription}.api.retailnext.net"


def subscription_name(text: str) -> str:
    """"acme", "acme.api.retailnext.net" or "https://acme.retailnext.net/x" -> "acme"."""
    s = re.sub(r"^https?://", "", text.strip().lower()).split("/")[0].split(".")[0]
    if not _SUBSCRIPTION.match(s):
        raise RetailNextError(f"{text!r} is not a RetailNext subscription name (the first "
                              f"part of your RetailNext web address)")
    return s


_PASTE_MARKS = re.compile(r"\x1b\[20[01]~|[\x00-\x1f\x7f]")  # bracketed paste, control keys


def clean_key(text: str) -> str:
    """A key as pasted: terminals can wrap a paste in invisible marks, which would make
    RetailNext reject a correct key."""
    return _PASTE_MARKS.sub("", text).strip()


def save_connection(subscription: str, access_key: str, secret_key: str) -> Connection:
    conn = Connection(subscription_name(subscription), clean_key(access_key),
                      clean_key(secret_key))
    if not conn.access_key or not conn.secret_key:
        raise RetailNextError("Both the access key and the secret key are needed.")
    keyring.set_password(SERVICE, conn.subscription, json.dumps(
        {"access_key": conn.access_key, "secret_key": conn.secret_key}))
    paths.save_settings({"retailnext_subscription": conn.subscription})
    return conn


def load_connection() -> Connection | None:
    sub = str(paths.load_settings().get("retailnext_subscription") or "")
    if not sub:
        return None
    try:
        raw = keyring.get_password(SERVICE, sub)
        data = json.loads(raw) if raw else None
    except (keyring.errors.KeyringError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("access_key") or not data.get("secret_key"):
        return None
    return Connection(sub, str(data["access_key"]), str(data["secret_key"]))


def forget_connection() -> None:
    sub = str(paths.load_settings().get("retailnext_subscription") or "")
    if sub:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(SERVICE, sub)
    paths.save_settings({"retailnext_subscription": ""})


_KEY_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def key_problems(conn: Connection) -> list[str]:
    """The usual mistakes in a stored key, found without showing any of it."""
    out = []
    access, secret = conn.access_key.lower(), conn.secret_key.lower()
    if _KEY_SHAPE.match(secret) and not _KEY_SHAPE.match(access):
        out.append("The secret key has the shape of an access key: the two look swapped.")
    elif not _KEY_SHAPE.match(access):
        out.append(f"The access key ({len(conn.access_key)} characters) does not have an access "
                   f"key's shape (8-4-4-4-12 letters and digits, like the one on the token page).")
    if re.search(r"\s", conn.access_key + conn.secret_key):
        out.append("There is a space or line break inside a key.")
    if odd := [i + 1 for i, ch in enumerate(conn.secret_key) if not re.match(r"[A-Za-z0-9_-]", ch)]:
        where = ", ".join(str(i) for i in odd)
        out.append(f"The secret key has a character other than letters, digits, - and _ at "
                   f"position {where} of {len(conn.secret_key)}: a secret key has none. Compare "
                   f"that character with the token page and enter the key again.")
    if not 16 <= len(conn.secret_key) <= 64:
        out.append(f"The secret key is {len(conn.secret_key)} characters long, which is unusual.")
    return out


def _context() -> ssl.SSLContext:
    try:
        import certifi  # the same trusted certificates on every computer
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def request(conn: Connection, path: str, body: dict[str, Any]) -> Any:
    """POST a query; retries RetailNext's own hiccups (5xx, timeouts), never 401/403.
    Messages never contain the keys."""
    token = base64.b64encode(f"{conn.access_key}:{conn.secret_key}".encode()).decode()
    req = urllib.request.Request(
        f"{conn.base}/{path}", data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Basic {token}", "Accept": "application/json",
                 "Content-Type": "application/json"})
    for attempt in range(RETRIES):
        last = attempt + 1 == RETRIES
        try:
            with _open(req, timeout=TIMEOUT_S, context=_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                said = e.read().decode("utf-8", "replace").strip()[:200]
                raise RetailNextError(
                    f"RetailNext ({conn.base}) refused the key (HTTP {e.code})"
                    f"{f': {said}' if said else ''}. The key may be mistyped, swapped with the "
                    f"secret, revoked, or for another subscription. 'retailnext.py check' "
                    f"looks for the usual mistakes; 'retailnext.py connect' enters it again.") from None
            if e.code >= 500 and not last:
                time.sleep(2 ** attempt)
                continue
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RetailNextError(f"RetailNext answered HTTP {e.code} to {path}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if not last:
                time.sleep(2 ** attempt)
                continue
            raise RetailNextError(
                f"Could not reach {conn.base} ({getattr(e, 'reason', e)}). Check the internet "
                f"connection and the subscription name.") from None
        except ValueError:
            raise RetailNextError(f"RetailNext's answer to {path} was not JSON.") from None
    raise RetailNextError(f"No answer from RetailNext to {path}.")


def locations(conn: Connection, types: list[str] | None = None) -> list[dict[str, Any]]:
    """Every location the key can see (stores, and whatever else RetailNext lists)."""
    body: dict[str, Any] = {"extra_fields": ["store.store_id", "store.time_zone"]}
    if types:
        body["where"] = {"and": [{"location_types": types}]}
    data = request(conn, "v2/location", body)
    groups = data.get("nodes", []) if isinstance(data, dict) else []
    return [n for g in groups for n in (g if isinstance(g, list) else [g]) if isinstance(n, dict)]


def _utc(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def traffic_request(location_uuids: list[str], day: date, start: str, until: str,
                    minutes: int = 15, time_zone: str | None = None) -> dict[str, Any]:
    """Traffic in and out for part of one day, per `minutes` ("HH:MM" from and until).

    With the store's time zone (IANA, from v2/location), the day is that store's local
    day, given in UTC as the API's gregorian dates are, and the answer's times are the
    store's own, as the API documents.
    """
    if time_zone:
        tz = ZoneInfo(time_zone)
        frm = _utc(datetime.combine(day, datetime.min.time(), tz))
        to = _utc(datetime.combine(day + timedelta(days=1), datetime.min.time(), tz))
    else:
        frm, to = f"{day.isoformat()}T00:00:00Z", f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z"
    body: dict[str, Any] = {
        "metrics": ["traffic_in", "traffic_out"],
        "date_ranges": [{"from": {"gregorian": frm}, "to": {"gregorian": to}}],
        "time_ranges": [{"from": start, "until": until}],
        "group_bys": [{"group": "time", "unit": "minutes", "value": minutes}],
        "locations": location_uuids}
    if time_zone:
        body["time_zone"] = time_zone
    return body


def traffic(conn: Connection, location_uuids: list[str], day: date, start: str, until: str,
            minutes: int = 15, time_zone: str | None = None) -> Any:
    return request(conn, "v2/datamine",
                   traffic_request(location_uuids, day, start, until, minutes, time_zone))


def _when(x: Any) -> str | None:
    if x is None:
        return None
    if isinstance(x, dict):
        return str(x.get("gregorian") or x.get("time") or json.dumps(x, sort_keys=True))
    return str(x)


def traffic_table(answer: Any) -> list[dict[str, Any]]:
    """A datamine answer grouped by time, as rows: start, finish, in, out, validity.

    Validity is "complete" unless RetailNext marked a value incomplete or imputed (a
    sensor down, say): such an interval is not a fair comparison. A metric RetailNext
    could not serve (ok false: usually a location the key does not cover) is an error.
    """
    if not isinstance(answer, dict):
        raise RetailNextError("RetailNext's answer was not what the API documents.")
    if answer.get("ok") is False:
        raise RetailNextError(f"RetailNext could not serve the data: "
                              f"{answer.get('error_detail') or answer.get('error') or 'no reason given'}")
    names = {"traffic_in": "in", "traffic_out": "out"}
    rows: dict[str, dict[str, Any]] = {}
    for metric in answer.get("metrics", []):
        key = names.get(str(metric.get("name")))
        if key is None:
            continue
        if metric.get("ok") is False:
            raise RetailNextError(f"RetailNext could not serve {metric.get('name')}: "
                                  f"{metric.get('error_detail') or metric.get('error') or 'no reason given'} "
                                  f"(the key's location profile may not include this location)")
        for point in metric.get("data", []):
            group = point.get("group") or {}
            start = _when(group.get("start") or group.get("from"))
            finish = _when(group.get("finish") or group.get("through") or group.get("until"))
            row = rows.setdefault(start or f"#{point.get('index')}", {
                "start": start, "finish": finish, "in": None, "out": None, "validity": "complete"})
            row[key] = point.get("value")
            if point.get("validity") not in (None, "complete"):
                row["validity"] = str(point["validity"])
    return sorted(rows.values(), key=lambda r: str(r["start"]))


def find_store(nodes: list[dict[str, Any]], code: str) -> dict[str, Any]:
    """The store with this code: RetailNext's store id (e.g. CN-123), else its name."""
    c = code.strip().lower()
    stores = [n for n in nodes if n.get("location_type") == "store"]
    hits = [n for n in stores if c and str(n.get("store_id") or "").lower() == c]
    hits = hits or [n for n in stores if c and c in str(n.get("name", "")).lower()]
    if len(hits) != 1:
        found = ", ".join(str(n.get("name")) for n in hits[:5])
        raise RetailNextError(f"RetailNext has {len(hits) or 'no'} store(s) with the code "
                              f"{code!r}{f': {found}' if found else ''}.")
    return hits[0]


def entrances(nodes: list[dict[str, Any]], store_uuid: str) -> dict[str, dict[str, Any]]:
    """A store's entrances by name: one per sensor, named like it (CN-123-PB1)."""
    return {str(n.get("name")): n for n in nodes
            if n.get("location_type") == "entrance" and n.get("parent_uuid") == store_uuid}


def camera_counts(conn: Connection, nodes: list[dict[str, Any]], code: str, cameras: list[str],
                  start: datetime, end: datetime, minutes: int = 15) -> dict[str, Any]:
    """RetailNext's own rows (retailnext.traffic_table) for each camera over the footage's
    period, from the store's entrance of the same name, in the store's time zone."""
    store = find_store(nodes, code)
    ents = entrances(nodes, str(store["uuid"]))
    lower = {name.lower(): node for name, node in ents.items()}
    missing = [c for c in cameras if c.lower() not in lower]
    if missing:
        raise RetailNextError(f"RetailNext's {store.get('name')} has no entrance called "
                              f"{', '.join(missing)} (it has {', '.join(sorted(ents)) or 'none'}): "
                              f"name the cameras as RetailNext does.")
    day, frm, until = period_of(start, end, minutes)
    tz = store.get("time_zone")
    return {"store": store.get("name"), "store_uuid": store["uuid"], "time_zone": tz,
            "day": day.isoformat(), "from": frm, "until": until,
            "cameras": {c: traffic_table(traffic(conn, [str(lower[c.lower()]["uuid"])], day, frm,
                                                 until, minutes, tz)) for c in cameras}}


def period_of(start: datetime, end: datetime, minutes: int = 15) -> tuple[date, str, str]:
    """The day and whole intervals ("HH:MM" from and until) that cover [start, end]."""
    if end.date() != start.date() and end.time() != datetime.min.time():
        raise RetailNextError("Footage that runs past midnight is not supported yet.")
    first = start.replace(minute=start.minute - start.minute % minutes, second=0, microsecond=0)
    last = end if end.minute % minutes == 0 and end.second == 0 else (
        end.replace(minute=end.minute - end.minute % minutes, second=0, microsecond=0)
        + timedelta(minutes=minutes))
    until = "24:00" if last.date() != first.date() else f"{last:%H:%M}"
    return first.date(), f"{first:%H:%M}", until


def save_raw(name: str, data: Any) -> Path:
    """Keep an answer as it came, on this computer (retailnext/ in the data folder)."""
    folder = paths.data_root() / "retailnext"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9_-]+', '-', name)}.json"
    write_json_atomic(out, data)
    return out
