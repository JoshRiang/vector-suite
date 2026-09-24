"""Rank a todo list and lay it out across a real day.

WHAT THIS IS
------------
The user's one recurring instruction: "create my schedule for today based on my
todo list ranked by priority (by you)". So the ranking is this module's job, not
the model's — a model asked to order 30 tasks will produce a different order on
every call, and a schedule that reshuffles itself each morning is useless.

RULES, IN THE ORDER THEY BIND
-----------------------------
1. A task blocked by unfinished work is never scheduled. It cannot be done.
2. Priority is ascending (1 = highest), matching the API's own ordering.
3. Among equal priority, the task that UNBLOCKS the most other work goes first.
   That is the whole point of ranking by hand rather than by insertion order.
4. Among equal priority and unblock value, shorter tasks first: more finished
   items for the same hour, and it leaves larger contiguous gaps intact.
5. Existing timed items are FIXED. A dentist appointment does not move because
   a task wants its slot; the task is placed around it.

REMINDER LEAD TIME
------------------
Chosen per task from its importance, not from one global setting: a task that
takes a day of preparation needs a day's notice, while a 10-minute chore only
needs a nudge. See lead_minutes().
"""
from __future__ import annotations

import json
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any

# The user asked for 5am to midnight: 20 rows, and late enough that an evening
# task is still visible without wasting rows on hours nobody uses.
DAY_START_HOUR = 5
DAY_END_HOUR = 24

# Gap left between consecutive placed tasks so a day is not one unbroken block.
BUFFER_MINUTES = 5

# A task needing more time than this in one sitting is still placed whole: a
# schedule that silently truncates work is worse than a long block.
MAX_BLOCK_MINUTES = 180

# How much notice each priority gets, in minutes. Priority 1 gets a day.
#
# This is the "you decide" part: importance determines the lead time, because a
# 15-minute warning is right for a chore and useless for something that needs
# preparation.
LEAD_BY_PRIORITY = {
    1: 24 * 60,   # critical: a day's notice
    2: 60,        # important: an hour
    3: 30,        # normal: half an hour
    4: 15,        # minor: a nudge
    5: 15,
}

# Tasks at or above this priority ALSO get a same-day reminder, so a day-ahead
# notice is not the only chance to catch it.
EXTRA_LEAD = {1: 60, 2: 15}


def lead_minutes(priority: Any) -> int:
    try:
        p = int(priority)
    except (TypeError, ValueError):
        p = 3
    return LEAD_BY_PRIORITY.get(p, 30)


def lead_times(priority: Any) -> list[int]:
    """All lead times for a task, longest first (e.g. [1440, 60])."""
    p = int(priority) if str(priority).isdigit() else 3
    out = [lead_minutes(p)]
    if p in EXTRA_LEAD:
        out.append(EXTRA_LEAD[p])
    return sorted(set(out), reverse=True)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    return None


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:00")


def rank(tasks: list[dict], all_tasks: list[dict]) -> list[dict]:
    """Order todo tasks by what actually deserves the next free hour.

    `all_tasks` is the full list, used to count how much other work each task
    unblocks — the signal that insertion order cannot express.
    """
    done_ids = {t.get("id") for t in all_tasks
                if str(t.get("status")) == "done"}
    unblocks: dict[str, int] = {}
    for t in all_tasks:
        blocker = t.get("blocked_by")
        if blocker:
            unblocks[str(blocker)] = unblocks.get(str(blocker), 0) + 1

    def startable(t: dict) -> bool:
        b = t.get("blocked_by")
        return not b or str(b) in done_ids

    def key(t: dict):
        try:
            prio = int(t.get("priority") or 3)
        except (TypeError, ValueError):
            prio = 3
        try:
            mins = int(t.get("minutes") or 30)
        except (TypeError, ValueError):
            mins = 30
        return (
            0 if startable(t) else 1,      # blocked work goes last, never first
            prio,                           # 1 = highest
            -unblocks.get(str(t.get("id")), 0),   # unblocks most first
            mins,                           # shorter first
            str(t.get("title") or ""),      # stable, not random
        )

    return sorted([t for t in tasks if str(t.get("status")) != "done"], key=key)


def build_day(user_id: str, day: str | None = None,
              start_hour: int = DAY_START_HOUR,
              end_hour: int = DAY_END_HOUR,
              apply: bool = True) -> dict[str, Any]:
    """Lay the user's open work across one day and (optionally) save it."""
    import store

    target = day or _date.today().isoformat()
    rows = store.db_request("GET", "tasks", params={"user_id": f"eq.{user_id}"})
    rows = rows if isinstance(rows, list) else rows.get("data", [])
    if not rows:
        return {"date": target, "planned": [], "skipped": [],
                "unscheduled_minutes": 0, "reason": "no_tasks"}

    # Anything with a time on a DIFFERENT day is not this day's business.
    #
    # On this day, the distinction that matters is WHO chose the time: an
    # appointment the user set is fixed, while a block this scheduler placed on
    # a previous run must stay movable. Otherwise re-running the plan finds the
    # whole day "busy" with its own output and piles every remaining task onto
    # the first free hour.
    fixed, movable = [], []
    for t in rows:
        when = _parse_dt(t.get("scheduled_at"))
        if when is None:
            movable.append(t)
        elif when.date().isoformat() != target:
            continue  # scheduled elsewhere; leave it alone entirely.
        elif int(t.get("auto_scheduled") or 0) == 1:
            movable.append(t)  # our own previous placement
        else:
            fixed.append(t)

    ordered = rank(movable, rows)

    # Build the free/busy map for the day.
    day_start = datetime.strptime(f"{target} {start_hour:02d}:00:00",
                                  "%Y-%m-%d %H:%M:%S")
    # Hour 24 means midnight at the END of the day, i.e. the next day's 00:00.
    # Parsing "24" as an hour is invalid and parsing the day's own 00:00 puts the
    # end BEFORE the start, which silently leaves no room to place anything.
    if end_hour >= 24:
        day_end = day_start.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    else:
        day_end = datetime.strptime(f"{target} {end_hour:02d}:00:00",
                                    "%Y-%m-%d %H:%M:%S")

    busy: list[tuple[datetime, datetime]] = []
    for t in fixed:
        when = _parse_dt(t.get("scheduled_at"))
        # `fixed` was built by selecting rows whose parsed time matches this
        # day, so a None here is impossible; guard anyway rather than let a
        # malformed row crash the whole schedule.
        if when is None:
            continue
        mins = int(t.get("minutes") or 30)
        busy.append((when, when + timedelta(minutes=mins)))
    busy.sort()

    def free_at(cursor: datetime, mins: int) -> datetime | None:
        """Earliest slot at or after `cursor` that fits `mins` uninterrupted."""
        probe = cursor
        for b_start, b_end in busy:
            if probe + timedelta(minutes=mins) <= b_start:
                return probe
            if probe < b_end:
                probe = b_end + timedelta(minutes=BUFFER_MINUTES)
        if probe + timedelta(minutes=mins) <= day_end:
            return probe
        return None

    cursor = day_start
    planned, skipped = [], []
    for t in ordered:
        # A task blocked by unfinished work cannot be done today; say so rather
        # than hiding it, so the reason is visible instead of the task vanishing.
        blocker = t.get("blocked_by")
        if blocker and str(blocker) not in {r.get("id") for r in rows
                                            if str(r.get("status")) == "done"}:
            skipped.append({"id": t.get("id"), "title": t.get("title"),
                            "reason": "blocked"})
            continue

        mins = int(t.get("minutes") or 30)
        mins = min(mins, MAX_BLOCK_MINUTES)
        slot = free_at(cursor, mins)
        if slot is None:
            skipped.append({"id": t.get("id"), "title": t.get("title"),
                            "reason": "no_time_left"})
            continue

        end = slot + timedelta(minutes=mins)
        busy.append((slot, end))
        busy.sort()
        cursor = end + timedelta(minutes=BUFFER_MINUTES)

        try:
            prio = int(t.get("priority") or 3)
        except (TypeError, ValueError):
            prio = 3
        planned.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "minutes": mins,
            "priority": prio,
            "scheduled_at": _fmt(slot),
            "reminder_leads": lead_times(prio),
        })

    # The day as a whole, fixed items included: a "schedule for today" that
    # omits an existing appointment is not a schedule of that day.
    fixed_out = []
    for t in fixed:
        when = _parse_dt(t.get("scheduled_at"))
        if when is None:
            continue
        try:
            prio = int(t.get("priority") or 3)
        except (TypeError, ValueError):
            prio = 3
        fixed_out.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "minutes": int(t.get("minutes") or 30),
            "priority": prio,
            "scheduled_at": _fmt(when),
            "reminder_leads": lead_times(prio),
            "fixed": True,
        })

    if apply:
        for item in planned:
            store.db_request("PATCH", "tasks", params={"id": f"eq.{item['id']}"},
                             body={"scheduled_at": item["scheduled_at"],
                                   "reminders": json.dumps(item["reminder_leads"]),
                                   # Marks this time as OUR choice, so the next
                                   # run may move it. A user-set time never gets
                                   # this flag and is therefore never moved.
                                   "auto_scheduled": 1})

    used = sum(p["minutes"] for p in planned)
    everything = sorted(fixed_out + planned, key=lambda p: p["scheduled_at"])
    return {
        "date": target,
        "planned": everything,
        "moved": planned,
        "fixed_count": len(fixed_out),
        "skipped": skipped,
        "planned_count": len(planned),
        "planned_minutes": used,
        "unscheduled_minutes": sum(
            int(s.get("minutes") or 30) for s in skipped
            if s.get("reason") == "no_time_left"),
        "day_start": _fmt(day_start),
        "day_end": _fmt(day_end),
    }


def due_reminders(user_id: str, now: datetime | None = None) -> list[dict]:
    """Reminders that should fire, from each task's own lead times."""
    import store

    now = now or datetime.now()
    rows = store.db_request("GET", "tasks", params={"user_id": f"eq.{user_id}"})
    rows = rows if isinstance(rows, list) else rows.get("data", [])

    out = []
    for t in rows:
        if str(t.get("status")) == "done":
            continue
        when = _parse_dt(t.get("scheduled_at"))
        if when is None:
            continue
        try:
            leads = json.loads(t.get("reminders") or "[]")
        except (TypeError, ValueError):
            leads = []
        if not isinstance(leads, list) or not leads:
            try:
                prio = int(t.get("priority") or 3)
            except (TypeError, ValueError):
                prio = 3
            leads = lead_times(prio)
        for lead in leads:
            fire = when - timedelta(minutes=int(lead))
            # A one-minute window: the caller runs on a schedule, and a wider
            # window would re-fire the same reminder on every pass.
            if timedelta(0) <= (now - fire) < timedelta(minutes=1):
                out.append({
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "at": _fmt(when),
                    "lead_minutes": int(lead),
                    "when": "tomorrow" if int(lead) >= 1440 else
                            f"in {int(lead)} min",
                })
    return out
