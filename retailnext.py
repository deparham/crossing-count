#!/usr/bin/env python3
"""RetailNext's own counts, from its cloud API (optional; only downloads).

    uv run retailnext.py connect      type the subscription name, access key and secret
                                      key; they are kept in this computer's credential
                                      store (Keychain, Credential Manager), then tested
    uv run retailnext.py locations    list the stores and other locations the key can see
    uv run retailnext.py traffic VIDEO [--location UUID] [--minutes 15]
                                      RetailNext's traffic in and out for the video's
                                      period; the answer is saved in retailnext/ in the
                                      data folder, as it came
    uv run retailnext.py check        look for the usual mistakes, without showing the key
    uv run retailnext.py forget       remove the key from this computer

Nothing is sent to RetailNext but these queries.
"""

from __future__ import annotations

import argparse
import getpass
import re
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from crossing_count import video as vid
from crossing_count.retailnext import (
    Connection,
    RetailNextError,
    forget_connection,
    key_problems,
    load_connection,
    locations,
    period_of,
    save_connection,
    save_raw,
    traffic,
)


def need() -> Connection:
    conn = load_connection()
    if conn is None:
        raise RetailNextError("Not connected yet: run 'retailnext.py connect' first.")
    return conn


def store_for(conn: Connection, video: Path) -> dict[str, Any]:
    """The store whose name or id carries the code in the video's name (e.g. YD-612)."""
    m = re.search(r"\b([A-Z]{2,4})-(\d{2,5})\b", video.name)
    if not m:
        raise RetailNextError("No store code (like YD-612) in the video's name: pass --location.")
    code, number = m.group(0), m.group(2)
    stores = locations(conn, ["store"])
    hits = [s for s in stores if code.lower() in str(s.get("name", "")).lower()
            or str(s.get("store_id", "")) in (code, number)]
    if len(hits) != 1:
        names = ", ".join(f"{s.get('name')} ({s.get('uuid')})" for s in hits[:10]) or "none"
        raise RetailNextError(f"{len(hits)} stores match {code}: {names}. Pass --location UUID.")
    return hits[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect")
    c.add_argument("--show-secret", action="store_true",
                   help="show the secret key while typing it, to compare with the token page")
    sub.add_parser("locations")
    t = sub.add_parser("traffic")
    t.add_argument("video", type=Path)
    t.add_argument("--location", help="a location's uuid, from 'locations'")
    t.add_argument("--minutes", type=int, default=15)
    sub.add_parser("check")
    sub.add_parser("forget")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check":
            conn = need()
            host = conn.base.removeprefix("https://")
            print(f"Subscription: {conn.subscription} ({host})")
            try:
                socket.getaddrinfo(host, 443)
            except socket.gaierror:
                print("  that address does not exist: the subscription name is probably wrong")
            for problem in key_problems(conn) or ["The keys have the expected shape."]:
                print(f"  {problem}")
            try:
                print(f"RetailNext accepted the key: it sees {len(locations(conn, ['store']))} "
                      f"store(s).")
            except RetailNextError as e:
                print(e)
                return 1
            return 0
        if args.cmd == "connect":
            conn = save_connection(input("Subscription name (the first part of your RetailNext "
                                         "web address): "),
                                   input("Access key: "),
                                   input("Secret key: ") if args.show_secret
                                   else getpass.getpass("Secret key (hidden; paste with Cmd+V): "))
            stores = locations(conn, ["store"])
            print(f"Connected to {conn.subscription}: the key sees {len(stores)} store(s). "
                  f"It is kept in this computer's credential store.")
        elif args.cmd == "locations":
            nodes = locations(need())
            for n in sorted(nodes, key=lambda n: (str(n.get("location_type")), str(n.get("name")))):
                print(f"{n.get('location_type', '?')!s:<14} {str(n.get('name', ''))[:44]:<44} "
                      f"{n.get('store_id') or ''!s:<8} {n.get('uuid')}")
            print(f"\n{len(nodes)} locations. Saved {save_raw('locations', nodes)}")
        elif args.cmd == "traffic":
            conn = need()
            span = vid.parse_filename_interval(args.video.name)
            if span is None:
                raise RetailNextError("The video's name has no start and end time.")
            day, start, until = period_of(datetime.fromisoformat(span.start),
                                          datetime.fromisoformat(span.end), args.minutes)
            if args.location:
                where, label = args.location, args.location
            else:
                store = store_for(conn, args.video)
                where, label = str(store["uuid"]), str(store.get("name"))
            print(f"{label}: {day} {start}-{until}, per {args.minutes} minutes")
            answer = traffic(conn, [where], day, start, until, args.minutes)
            print(f"Saved {save_raw('traffic', {'query': {'location': where, 'day': str(day), 'from': start, 'until': until, 'minutes': args.minutes}, 'answer': answer})}")
        elif args.cmd == "forget":
            forget_connection()
            print("The RetailNext key is no longer on this computer.")
    except RetailNextError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
