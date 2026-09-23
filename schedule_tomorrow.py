"""Schedule tomorrow's work so the calendar actually has a plan on it.

WHY: /calendar returned zero days because no task had a scheduled_at date. A
month grid with no scheduled work is an empty calendar - the widget can only
dot a day if something is really planned for it. This assigns the currently
startable task of each goal (the engine's own "do this next") to tomorrow, which
is the honest definition of tomorrow's plan: the work the engine says is next.

Idempotent: re-running moves the same tasks to the same date rather than
duplicating anything.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

BASE = os.environ.get("VECTOR_BASE_URL", "http://127.0.0.1:8790")
KEY = os.environ["VECTOR_API_KEY"]
UID = "josh"
H = {"X-Api-Key": KEY, "X-User-Id": UID, "Content-Type": "application/json"}

# Local start time for scheduled work.
START_HOUR = 9


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=H,
                                 method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else None


def main() -> int:
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    when = f"{tomorrow}T{START_HOUR:02d}:00:00"

    startable = call("GET", "/tasks/startable")
    if not startable:
        print("  no startable tasks - nothing to schedule")
        return 1

    print(f"  scheduling for {tomorrow} (tomorrow)\n")
    scheduled = []
    for t in startable:
        tid = t.get("id")
        if not tid:
            continue
        call("PATCH", "/tasks", {"id": tid, "scheduled_at": when})
        scheduled.append((t.get("title", "")[:60], t.get("minutes")))
        print(f"    + {t.get('title','')[:60]}  ({t.get('minutes')} min)")

    total = sum(m or 0 for _, m in scheduled)
    print(f"\n  {len(scheduled)} task(s), {total} min planned for {tomorrow}")

    cal = call("GET", "/calendar")
    print(f"\n  /calendar -> today={cal['today']} horizon={cal['horizon']}")
    for d in cal["days"]:
        print(f"    {d}")

    if not any(d["date"] == tomorrow for d in cal["days"]):
        print("\nFAIL: tomorrow does not appear in /calendar")
        return 1
    print("\nTOMORROW IS ON THE CALENDAR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
