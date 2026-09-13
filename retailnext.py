#!/usr/bin/env python3
"""RetailNext's own counts, from its cloud API (optional; only downloads).

    uv run retailnext.py connect [NAME]   add a subscription (one per customer): its name,
                                          access key and secret key, kept in this computer's
                                          credential store (Keychain, Credential Manager).
                                          For one whose key is already stored, nothing is
                                          typed; --new-key enters a new key.
    uv run retailnext.py list             the connected subscriptions
    uv run retailnext.py check [NAME]     test them all (or one), without showing any key
    uv run retailnext.py locations [--subscription NAME]
                                          the stores and other locations the keys can see
    uv run retailnext.py traffic VIDEO [--location UUID] [--subscription NAME] [--minutes 15]
                                          RetailNext's traffic for the video's period, from
                                          whichever subscription has its store; the answer is
                                          saved in retailnext/ in the data folder, as it came
    uv run retailnext.py forget NAME      remove that subscription's key from this computer

Everything else (the wizard included) uses whichever connected subscription has the store.
Nothing is sent to RetailNext but these queries.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from crossing_count.retailnext import (
    Connection,
    RetailNextError,
    clean_key,
    connections,
    forget_connection,
    key_problems,
    load_connection,
    locations,
    period_of,
    reconnect,
    save_connection,
    save_raw,
    subscription_name,
    subscriptions,
    traffic,
    traffic_table,
)


def ask_secret(show: bool) -> str:
    """The secret key, asked again while it holds a character RetailNext secrets never
    have (a misread or mistyped one): the most common reason for "bad password"."""
    while True:
        secret = clean_key(input("Secret key: ") if show
                           else getpass.getpass("Secret key (hidden; paste with Cmd+V): "))
        odd = [(i + 1, ch) for i, ch in enumerate(secret) if not re.match(r"[A-Za-z0-9_-]", ch)]
        if not odd:
            return secret
        where = ", ".join(f"{i}" + (f" ('{ch}')" if show else "") for i, ch in odd)
        print(f"Position {where} of {len(secret)} is not a letter, digit, - or _, and a "
              f"RetailNext secret key has only those. Look at that character on the token "
              f"page (text copied from a picture of it is often misread there).")
        if input("Type the secret key again? [Y/n] ").strip().lower() in ("n", "no"):
            return secret


def chosen(name: str | None) -> list[Connection]:
    """That subscription, or every connected one."""
    if name:
        conn = load_connection(subscription_name(name))
        if conn is None:
            raise RetailNextError(f"{name} is not connected on this computer: run "
                                  f"'retailnext.py connect {name}'.")
        return [conn]
    conns = connections()
    if not conns:
        raise RetailNextError("Not connected yet: run 'retailnext.py connect' first.")
    return conns


def store_for(conns: list[Connection], video: Path) -> tuple[Connection, dict[str, Any]]:
    """The store whose name or id carries the code in the video's name (e.g. YD-612), in
    whichever subscription has it."""
    m = re.search(r"\b([A-Z]{2,4})-(\d{2,5})\b", video.name)
    if not m:
        raise RetailNextError("No store code (like YD-612) in the video's name: pass --location.")
    code, number = m.group(0), m.group(2)
    hits = [(conn, s) for conn in conns for s in locations(conn, ["store"])
            if code.lower() in str(s.get("name", "")).lower()
            or str(s.get("store_id", "")) in (code, number)]
    if len(hits) != 1:
        names = ", ".join(f"{s.get('name')} ({c.subscription}, {s.get('uuid')})"
                          for c, s in hits[:10]) or "none"
        raise RetailNextError(f"{len(hits)} stores match {code}: {names}. Pass --location UUID "
                              f"(and --subscription).")
    return hits[0]


def check(conn: Connection) -> bool:
    print(f"{conn.subscription} ({conn.base.removeprefix('https://')})")
    for problem in key_problems(conn) or ["The keys have the expected shape."]:
        print(f"  {problem}")
    try:
        print(f"  RetailNext accepted the key: it sees {len(locations(conn, ['store']))} store(s).")
    except RetailNextError as e:
        print(f"  {e}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect")
    c.add_argument("name", nargs="?", help="the subscription, e.g. rag (asked if not given)")
    c.add_argument("--new-key", action="store_true", help="enter a new key even if one is stored")
    c.add_argument("--show-secret", action="store_true",
                   help="show the secret key while typing it, to compare with the token page")
    sub.add_parser("list")
    k = sub.add_parser("check")
    k.add_argument("name", nargs="?")
    loc = sub.add_parser("locations")
    loc.add_argument("--subscription")
    t = sub.add_parser("traffic")
    t.add_argument("video", type=Path)
    t.add_argument("--location", help="a location's uuid, from 'locations'")
    t.add_argument("--subscription", help="needed with --location when several are connected")
    t.add_argument("--minutes", type=int, default=15)
    t.add_argument("--time-zone", help="the store's IANA time zone, e.g. Australia/Brisbane "
                                       "(found by itself for a store)")
    f = sub.add_parser("forget")
    f.add_argument("name")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "connect":
            if args.name and not args.new_key and (conn := reconnect(args.name)) is not None:
                print(f"{conn.subscription}'s key is already on this computer: connected.")
            else:
                name = args.name or input("Subscription (in rag.cloud.retailnext.net it is "
                                          "'rag'; the whole address works too): ")
                conn = save_connection(name, input("Access key: "), ask_secret(args.show_secret))
                for problem in key_problems(conn):
                    print(f"Warning: {problem}")
            stores = locations(conn, ["store"])
            print(f"{conn.subscription}: the key sees {len(stores)} store(s). Connected: "
                  f"{', '.join(subscriptions())}.")
        elif args.cmd == "list":
            subs = subscriptions()
            for s in subs:
                print(f"  {s}" + ("" if load_connection(s) else
                                  "   (its key is not on this computer: connect it again)"))
            print("" if subs else "None connected: run 'retailnext.py connect'.")
        elif args.cmd == "check":
            results = [check(conn) for conn in chosen(args.name)]  # every one, even after a failure
            return 0 if all(results) else 1
        elif args.cmd == "locations":
            for conn in chosen(args.subscription):
                nodes = locations(conn)
                for n in sorted(nodes, key=lambda n: (str(n.get("location_type")), str(n.get("name")))):
                    print(f"{conn.subscription:<10} {n.get('location_type', '?')!s:<14} "
                          f"{str(n.get('name', ''))[:44]:<44} {n.get('store_id') or ''!s:<8} "
                          f"{n.get('uuid')}")
                print(f"\n{conn.subscription}: {len(nodes)} locations. Saved "
                      f"{save_raw(f'locations-{conn.subscription}', nodes)}\n")
        elif args.cmd == "traffic":
            from crossing_count import video as vid  # loads the video libraries: only here

            span = vid.parse_filename_interval(args.video.name)
            if span is None:
                raise RetailNextError("The video's name has no start and end time.")
            day, start, until = period_of(datetime.fromisoformat(span.start),
                                          datetime.fromisoformat(span.end), args.minutes)
            conns = chosen(args.subscription)
            tz = args.time_zone
            if args.location:
                if len(conns) > 1:
                    raise RetailNextError("Several subscriptions are connected: say which with "
                                          "--subscription.")
                conn, where, label = conns[0], args.location, args.location
            else:
                conn, store = store_for(conns, args.video)
                where, label = str(store["uuid"]), f"{store.get('name')} ({conn.subscription})"
                tz = tz or store.get("time_zone")
            print(f"{label}: {day} {start}-{until} ({tz or 'no time zone given'}), per "
                  f"{args.minutes} minutes")
            answer = traffic(conn, [where], day, start, until, args.minutes, tz)
            query = {"subscription": conn.subscription, "location": where, "day": str(day),
                     "from": start, "until": until, "minutes": args.minutes, "time_zone": tz}
            print(f"Saved {save_raw('traffic', {'query': query, 'answer': answer})}")
            for row in traffic_table(answer):
                flag = "" if row["validity"] == "complete" else f"  ({row['validity']})"
                print(f"  {row['start']} - {row['finish']}   in {row['in']}   out {row['out']}{flag}")
        elif args.cmd == "forget":
            name = subscription_name(args.name)
            forget_connection(name)
            print(f"{name}'s key is no longer on this computer. Connected: "
                  f"{', '.join(subscriptions()) or 'none'}.")
    except RetailNextError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
