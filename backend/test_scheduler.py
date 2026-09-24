"""The scheduler decides how a real day is ordered, so pin the rules.

Every test here is hermetic: a throwaway local database, no network, no model.
The ranking rules are the product, and a silent regression in them would show up
only as a subtly wrong day.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

for _k in ("DATABASE_URL", "SUPABASE_DB_URL", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
    os.environ.pop(_k, None)
os.environ["VECTOR_DB"] = os.path.join(tempfile.mkdtemp(), "sched.db")

import scheduler  # noqa: E402
import store  # noqa: E402

UID = "sched-probe"
PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def wipe() -> None:
    for r in store.db_request("GET", "tasks", params={"user_id": f"eq.{UID}"}) or []:
        store.db_request("DELETE", "tasks", params={"id": f"eq.{r['id']}"})


def add(title, minutes=30, priority=3, status="todo", scheduled_at=None,
        blocked_by=None):
    body = {"user_id": UID, "title": title, "minutes": minutes,
            "priority": priority, "status": status}
    if scheduled_at:
        body["scheduled_at"] = scheduled_at
    if blocked_by:
        body["blocked_by"] = blocked_by
    row = store.db_request("POST", "tasks", body=body)
    return row if isinstance(row, dict) else (row or [{}])[0]


TODAY = datetime.now().strftime("%Y-%m-%d")
TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

# --------------------------------------------------------------------- ranking
print("\n-- ranking --")
wipe()
low = add("low priority", priority=4)
high = add("high priority", priority=1)
mid = add("mid priority", priority=2)
rows = store.db_request("GET", "tasks", params={"user_id": f"eq.{UID}"})
order = [t["title"] for t in scheduler.rank(rows, rows)]
check("priority 1 sorts before 2 before 4",
      order == ["high priority", "mid priority", "low priority"], str(order))

wipe()
blocker = add("the blocker", priority=3, minutes=30)
blocked = add("depends on blocker", priority=1, blocked_by=blocker["id"])
rows = store.db_request("GET", "tasks", params={"user_id": f"eq.{UID}"})
order = [t["title"] for t in scheduler.rank(rows, rows)]
check("a blocked task never outranks its unblocked blocker, even at higher priority",
      order[0] == "the blocker", str(order))

wipe()
# A task that unblocks two others should beat an equal-priority task that
# unblocks none - this is the signal insertion order cannot express.
key = add("unblocks two", priority=2, minutes=60)
add("child a", priority=2, blocked_by=key["id"])
add("child b", priority=2, blocked_by=key["id"])
lone = add("unblocks none", priority=2, minutes=60)
rows = store.db_request("GET", "tasks", params={"user_id": f"eq.{UID}"})
order = [t["title"] for t in scheduler.rank(rows, rows)]
check("at equal priority, the task that unblocks the most goes first",
      order.index("unblocks two") < order.index("unblocks none"), str(order))

wipe()
add("long task", priority=3, minutes=120)
add("short task", priority=3, minutes=15)
rows = store.db_request("GET", "tasks", params={"user_id": f"eq.{UID}"})
order = [t["title"] for t in scheduler.rank(rows, rows)]
check("at equal priority, shorter work first",
      order[0] == "short task", str(order))

# ------------------------------------------------------------------- day layout
print("\n-- day layout --")
wipe()
add("task one", minutes=60, priority=1)
add("task two", minutes=60, priority=2)
res = scheduler.build_day(UID, day=TODAY, apply=False)
starts = [p["scheduled_at"] for p in res["planned"]]
check("two hours of work is planned", res["planned_minutes"] == 120,
      str(res["planned_minutes"]))
check("first block starts at the day start (05:00)",
      starts[0].endswith("T05:00:00"), starts[0])
check("blocks do not overlap",
      starts[1] > starts[0], f"{starts[0]} -> {starts[1]}")
check("a buffer separates the blocks",
      datetime.fromisoformat(starts[1]) - datetime.fromisoformat(starts[0])
      == timedelta(minutes=65), str(starts))
check("priority 1 is placed first",
      res["planned"][0]["title"] == "task one", str(res["planned"][0]["title"]))

wipe()
add("fixed meeting", minutes=60, scheduled_at=f"{TODAY}T10:00:00")
add("movable work", minutes=60, priority=1)
res = scheduler.build_day(UID, day=TODAY, apply=False)
by_title = {p["title"]: p for p in res["planned"]}
check("an existing timed item keeps its slot",
      by_title["fixed meeting"]["scheduled_at"] == f"{TODAY}T10:00:00",
      by_title["fixed meeting"]["scheduled_at"])
# The fixed item occupies 10:00-11:00, so a 60-minute task cannot start at 10:00.
check("a placed task is not put on top of a fixed item",
      by_title["movable work"]["scheduled_at"] != f"{TODAY}T10:00:00",
      by_title["movable work"]["scheduled_at"])

wipe()
add("day filler", minutes=600, priority=3)
res = scheduler.build_day(UID, day=TODAY, apply=False)
check("a long task is placed, not truncated", res["planned_minutes"] == 180,
      f"clamped to MAX_BLOCK_MINUTES: {res['planned_minutes']}")

wipe()
for i in range(12):
    add(f"bulk {i}", minutes=120, priority=3)
res = scheduler.build_day(UID, day=TODAY, apply=False)
check("work that does not fit is reported, not silently dropped",
      len(res["skipped"]) > 0, f"skipped={len(res['skipped'])}")
check("the reported overflow is a real number of minutes",
      res["unscheduled_minutes"] > 0, str(res["unscheduled_minutes"]))

wipe()
done = add("already done", status="done")
res = scheduler.build_day(UID, day=TODAY, apply=False)
check("completed work is not rescheduled",
      all(p["id"] != done["id"] for p in res["planned"]), str(res["planned"]))

wipe()
other = add("tomorrow's task", scheduled_at=f"{TOMORROW}T09:00:00")
res = scheduler.build_day(UID, day=TODAY, apply=False)
check("a task scheduled for another day is left alone",
      all(p["id"] != other["id"] for p in res["planned"]))

# --------------------------------------------------------------- reminder lead
print("\n-- reminder lead time --")
check("priority 1 gets a day's notice", scheduler.lead_minutes(1) == 1440,
      str(scheduler.lead_minutes(1)))
check("priority 2 gets an hour", scheduler.lead_minutes(2) == 60,
      str(scheduler.lead_minutes(2)))
check("priority 3 gets 30 minutes", scheduler.lead_minutes(3) == 30,
      str(scheduler.lead_minutes(3)))
check("priority 4 gets 15 minutes", scheduler.lead_minutes(4) == 15,
      str(scheduler.lead_minutes(4)))
check("a missing priority falls back to 30, not a crash",
      scheduler.lead_minutes(None) == 30, str(scheduler.lead_minutes(None)))
check("a critical task gets two reminders", len(scheduler.lead_times(1)) == 2,
      str(scheduler.lead_times(1)))
check("a minor task gets one reminder", len(scheduler.lead_times(4)) == 1,
      str(scheduler.lead_times(4)))

wipe()
soon = (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:00")
add("due in 30 min", priority=3, scheduled_at=soon)
due = scheduler.due_reminders(UID)
check("a 30-minute lead fires exactly when it should", len(due) == 1, str(due))
check("the reminder names the task", due and due[0]["title"] == "due in 30 min",
      str(due))

wipe()
far = (datetime.now() + timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:00")
add("due in 90 min", priority=3, scheduled_at=far)
far_due = scheduler.due_reminders(UID)
check("a reminder outside its window does not fire", len(far_due) == 0,
      str(far_due))

# ------------------------------------------------------------- apply behaviour
print("\n-- apply --")
wipe()
t = add("will be scheduled", minutes=30, priority=2)
res = scheduler.build_day(UID, day=TODAY, apply=True)
saved = store.db_request("GET", "tasks", params={"id": f"eq.{t['id']}"})
saved = saved[0] if isinstance(saved, list) and saved else {}
check("apply=True writes the time to the task",
      bool(saved.get("scheduled_at")), str(saved.get("scheduled_at")))
check("apply=True writes the reminder leads too",
      bool(saved.get("reminders")), str(saved.get("reminders")))
leads = json.loads(saved.get("reminders") or "[]")
check("the saved leads match the task's priority",
      leads == scheduler.lead_times(2), f"{leads} vs {scheduler.lead_times(2)}")

wipe()
t2 = add("not scheduled", minutes=30, priority=2)
scheduler.build_day(UID, day=TODAY, apply=False)
saved2 = store.db_request("GET", "tasks", params={"id": f"eq.{t2['id']}"})
saved2 = saved2[0] if isinstance(saved2, list) and saved2 else {}
check("apply=False changes nothing on disk",
      not saved2.get("scheduled_at"), str(saved2.get("scheduled_at")))

# ------------------------------------------------------------- re-run behaviour
print("\n-- re-running the plan --")
wipe()
add("task a", minutes=60, priority=1)
add("task b", minutes=60, priority=2)
first = scheduler.build_day(UID, day=TODAY, apply=True)
first_times = {p["title"]: p["scheduled_at"] for p in first["planned"]}

second = scheduler.build_day(UID, day=TODAY, apply=True)
second_times = {p["title"]: p["scheduled_at"] for p in second["planned"]}
check("a second run produces the SAME schedule (idempotent)",
      first_times == second_times, f"{first_times} vs {second_times}")
check("a second run does not drop tasks",
      len(second["planned"]) == len(first["planned"]),
      f"{len(first['planned'])} -> {len(second['planned'])}")

# The failure this guards: without an auto_scheduled flag, the second run sees
# its own output as fixed appointments and stacks everything onto one hour.
wipe()
add("only task", minutes=30, priority=3)
scheduler.build_day(UID, day=TODAY, apply=True)
again = scheduler.build_day(UID, day=TODAY, apply=True)
check("a re-run keeps the task at its own time, not a fallback hour",
      again["planned"][0]["scheduled_at"].endswith("T05:00:00"),
      again["planned"][0]["scheduled_at"])

# A user-set time must be immune: it is the whole reason the flag exists.
wipe()
mine = add("my own appointment", minutes=30, scheduled_at=f"{TODAY}T14:00:00")
scheduler.build_day(UID, day=TODAY, apply=True)
after = store.db_request("GET", "tasks", params={"id": f"eq.{mine['id']}"})
after = after[0] if isinstance(after, list) and after else {}
check("a user-set appointment is never moved by the scheduler",
      after.get("scheduled_at") == f"{TODAY}T14:00:00",
      str(after.get("scheduled_at")))
check("a user-set appointment is not marked auto_scheduled",
      int(after.get("auto_scheduled") or 0) == 0,
      str(after.get("auto_scheduled")))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
