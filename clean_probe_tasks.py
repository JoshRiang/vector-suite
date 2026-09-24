#!/usr/bin/env python3
"""Remove probe/test tasks that leaked into the user's real task list.

Running the tap and lock probes against the live API created real rows in the
user's own list ("WIDGET TAP PROBE", "CONCURRENCY TEST", "REMINDER PIPELINE
TEST", "DBG", "DBGSTATUS", "probe appointment"). A test that litters the user's
data is a bug in the test, so this deletes exactly those titles and reports what
it removed. It never touches a task the user created.

Usage: python3 clean_probe_tasks.py [--apply]
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8790"
ENV = "/home/josh/vector_suite/backend/.env"

# Prefixes only this project's probes use. A user task is never named these.
PROBE_PREFIXES = (
    "WIDGET TAP PROBE",
    "CONCURRENCY TEST",
    "REMINDER PIPELINE TEST",
    "DBGSTATUS",
    "DBG",
    "PROBE SCHED",
    "probe appointment",
)


def key() -> str:
    for line in open(ENV, encoding="utf-8"):
        line = line.strip()
        if line.startswith("VECTOR_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"X-API-Key": key(), "X-User-Id": "josh",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw


def main() -> int:
    apply = "--apply" in sys.argv
    _, rows = call("GET", "/tasks/startable")
    if not isinstance(rows, list):
        print(f"could not read tasks: {rows}")
        return 1

    victims = [r for r in rows
               if any(str(r.get("title", "")).startswith(p)
                      for p in PROBE_PREFIXES)]
    print(f"{len(rows)} open tasks; {len(victims)} are probe leftovers")
    for v in victims:
        print(f"  - {v.get('title')}  ({v.get('id')})")
        if apply:
            call("DELETE", "/tasks", {"id": v["id"]})

    if not apply:
        print("\ndry run - re-run with --apply to delete")
        return 0

    _, after = call("GET", "/tasks/startable")
    left = [r for r in (after if isinstance(after, list) else [])
            if any(str(r.get("title", "")).startswith(p)
                   for p in PROBE_PREFIXES)]
    print(f"\nafter: {len(left)} probe leftovers remaining")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
