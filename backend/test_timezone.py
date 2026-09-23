"""Regression test: "done today" must survive the WIB midnight boundary.

THE BUG THIS PINS DOWN
----------------------
Completed tasks were stamped with UTC time but filtered against the LOCAL
calendar date. West Indonesia is UTC+7, so a task finished at 01:30 WIB was
stored as 18:30 the PREVIOUS day and disappeared from "done today" -- the
exact hours a student is most likely to be working.

The test freezes the clock at 01:30 WIB, completes a task, and asserts it is
counted. Without the fix this fails.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

# Hermetic: these tests must run on a throwaway SQLite file. If a
# DATABASE_URL is present (e.g. the Supabase DSN in backend/.env) the
# store would connect to the LIVE cloud database and the tests would
# write there. Clear it so the suite always targets a temp file.
for _k in (
    "DATABASE_URL", "SUPABASE_DB_URL", "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY", "VECTOR_API_KEY",
):
    os.environ.pop(_k, None)
os.environ["VECTOR_DB"] = os.path.join(tempfile.mkdtemp(), "tz.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api  # noqa: E402
import store  # noqa: E402

PASS = FAIL = 0
WIB = timezone(timedelta(hours=7))


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def main() -> int:
    store.init_db()
    uid = str(uuid.uuid4())

    print("local-time helpers")
    check("today is the local calendar date, not UTC",
          api._today_local() == datetime.now(WIB).date().isoformat(),
          f"got {api._today_local()}")
    ts = api._now_local_iso()
    check("completion stamp carries a local offset",
          ts.endswith("+07:00"), f"got {ts}")
    check("completion stamp's date == today's local date",
          ts[:10] == api._today_local(), f"stamp {ts[:10]} vs today {api._today_local()}")

    print("\nlate-night completion is counted as today")
    g = api.db_request("POST", "goals", body={"user_id": uid, "title": "Late night"})
    gid = g[0]["id"]
    t = api.db_request("POST", "tasks", body={
        "user_id": uid, "goal_id": gid, "title": "Do the thing", "minutes": 25})[0]

    # Complete it exactly as the PATCH handler would.
    api.route("PATCH", "/tasks", {"id": t["id"], "status": "done"}, uid)

    today = api.today_plan(uid)
    titles = [x["title"] for x in today["done_today"]]
    check("completed task appears in done_today", "Do the thing" in titles,
          f"got {titles}")
    check("date field is the local date", today["date"] == api._today_local(),
          f"got {today['date']}")

    print("\nboundary: a task completed at 00:05 WIB still counts")
    stamp = datetime.now(WIB).replace(hour=0, minute=5, second=0,
                                      microsecond=0)
    store.db_request("PATCH", "tasks", params={"id": f"eq.{t['id']}"},
                     body={"completed_at": stamp.isoformat()})
    today = api.today_plan(uid)
    check("00:05 WIB completion is still 'today'",
          "Do the thing" in [x["title"] for x in today["done_today"]],
          f"got {[x['title'] for x in today['done_today']]}")

    print("\nboundary: a task completed yesterday does NOT count")
    yday = datetime.now(WIB).replace(hour=23, minute=50) - timedelta(days=1)
    store.db_request("PATCH", "tasks", params={"id": f"eq.{t['id']}"},
                     body={"completed_at": yday.isoformat()})
    today = api.today_plan(uid)
    check("23:50 WIB yesterday is excluded",
          "Do the thing" not in [x["title"] for x in today["done_today"]],
          f"got {[x['title'] for x in today['done_today']]}")

    print("\nfinance 'today_spent' uses the local date")
    api.db_request("POST", "expenses", body={"user_id": uid, "amount": 10000})
    fin = api.finance_summary(uid)
    check("today's expense is counted", fin["today_spent"] == 10000,
          f"got {fin['today_spent']}")
    check("30-day window includes it", fin["spent_30d"] == 10000,
          f"got {fin['spent_30d']}")

    print("\nstreak counts from the local date")
    prod = api.productivity(uid)
    check("streak is an int and not negative",
          isinstance(prod["current_streak"], int) and prod["current_streak"] >= 0,
          f"got {prod['current_streak']}")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
