"""The calendar must be able to show days other than today.

The widget previously derived its dots from /today alone, so it could only ever
mark the CURRENT day - tomorrow's scheduled work was invisible, which makes a
calendar useless. /calendar exists to fix that, and these pin the properties
that matter: a future date appears, a date outside the window does not, and
scheduled vs done stay distinguishable.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Hermetic: never touch a live database. Without this the suite runs against
# production Postgres and times out on statement limits.
for _k in ("DATABASE_URL", "SUPABASE_DB_URL", "SUPABASE_URL",
           "SUPABASE_SERVICE_KEY", "VECTOR_API_KEY"):
    os.environ.pop(_k, None)
os.environ["VECTOR_DB"] = os.path.join(tempfile.mkdtemp(), "calendar.db")

import api  # noqa: E402
import store  # noqa: E402

UID = "calendar-test-user"


def _clean():
    for t in store.db_request("GET", "tasks",
                              params={"user_id": f"eq.{UID}"}) or []:
        store.db_request("DELETE", "tasks", params={"id": f"eq.{t['id']}"})


def test_empty_calendar_is_valid() -> list[str]:
    _clean()
    out = api.calendar_month(UID)
    fails = []
    if out.get("days") != []:
        fails.append(f"expected no days, got {out.get('days')}")
    if not out.get("today") or not out.get("horizon"):
        fails.append("today/horizon missing")
    return fails


def test_tomorrow_appears() -> list[str]:
    """The exact bug: a scheduled task must dot a FUTURE day, not only today."""
    _clean()
    fails = []
    tomorrow = (datetime.now(api.LOCAL_TZ).date() + timedelta(days=1)).isoformat()
    store.db_request("POST", "tasks", body={
        "user_id": UID, "title": "tomorrow work", "minutes": 30,
        "status": "todo", "priority": 3,
        "scheduled_at": f"{tomorrow}T09:00:00",
    })
    out = api.calendar_month(UID)
    days = {d["date"]: d for d in out["days"]}
    if tomorrow not in days:
        fails.append(f"tomorrow {tomorrow} missing from {sorted(days)}")
    else:
        if days[tomorrow]["scheduled"] != 1:
            fails.append(f"scheduled count wrong: {days[tomorrow]}")
        if days[tomorrow]["minutes"] != 30:
            fails.append(f"minutes wrong: {days[tomorrow]}")
    return fails


def test_beyond_horizon_is_excluded() -> list[str]:
    _clean()
    fails = []
    far = (datetime.now(api.LOCAL_TZ).date() + timedelta(days=400)).isoformat()
    store.db_request("POST", "tasks", body={
        "user_id": UID, "title": "far future", "minutes": 30,
        "status": "todo", "priority": 3, "scheduled_at": f"{far}T09:00:00",
    })
    out = api.calendar_month(UID)
    if any(d["date"] == far for d in out["days"]):
        fails.append(f"date beyond horizon {far} was included")
    return fails


def test_done_and_scheduled_are_separate() -> list[str]:
    """A completed task is a record, a scheduled one is a plan."""
    _clean()
    fails = []
    today = datetime.now(api.LOCAL_TZ).date().isoformat()
    store.db_request("POST", "tasks", body={
        "user_id": UID, "title": "finished", "minutes": 10,
        "status": "done", "priority": 3,
        "completed_at": f"{today}T10:00:00",
    })
    out = api.calendar_month(UID)
    days = {d["date"]: d for d in out["days"]}
    if today not in days:
        fails.append(f"today with a completed task missing from {sorted(days)}")
    elif days[today]["done"] != 1:
        fails.append(f"done count wrong: {days[today]}")
    return fails


def main() -> int:
    total = failed = 0
    for fn in (test_empty_calendar_is_valid, test_tomorrow_appears,
               test_beyond_horizon_is_excluded, test_done_and_scheduled_are_separate):
        total += 1
        fails = fn()
        if fails:
            failed += 1
            print(f"FAIL {fn.__name__}")
            for f in fails:
                print(f"       {f}")
        else:
            print(f"  ok {fn.__name__}")
    _clean()
    print(f"\n{total - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
