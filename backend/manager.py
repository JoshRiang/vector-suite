"""The one entry point Hermes runs to manage the user's day.

Before this existed the morning was split across three scripts (`daily_plan.py`,
`daily_brief.py`, `schedule_tomorrow.py`) that each built their own view of the
same data. Two planners over one task list drift apart, and when they disagree
the user cannot tell which one is right -- the calendar says one thing and the
brief says another, and both are "correct".

So this module owns the morning. It calls the existing, already-tested pieces
(`scheduler.build_day`, `daily_brief.build_brief`, `scheduler.due_reminders`)
and does not reimplement any of their logic. The other scripts stay on disk and
stay runnable by hand, but only this one is scheduled.

Usage:
    python3 manager.py                 # plan + brief to Telegram
    python3 manager.py --dry-run       # print only, write nothing
    python3 manager.py --no-send       # apply, but do not message
    python3 manager.py --snapshot      # print the one-call state summary
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime

import daily_brief
import scheduler

# The user this deployment manages. Overridable so a second deployment does not
# silently operate on the first one's rows.
USER_ID = os.environ.get("VECTOR_USER", "josh")


def snapshot() -> dict:
    """Everything Hermes needs about today in one call.

    Reuses `api` for the read paths rather than querying tables here, so this
    summary cannot disagree with what the apps themselves display.
    """
    import api

    out: dict = {"user_id": USER_ID, "date": date.today().isoformat()}
    for key, fn in (("today", api.today_plan), ("finance", api.finance_summary)):
        try:
            out[key] = fn(USER_ID)
        except Exception as exc:  # noqa: BLE001
            # A failing section must not take the whole snapshot down: a partial
            # summary the user can act on beats no summary at all.
            out[key] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        out["due_reminders"] = scheduler.due_reminders(USER_ID)
    except Exception as exc:  # noqa: BLE001
        out["due_reminders"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def send(text: str) -> int:
    """Send to Telegram via the hermes CLI, reporting failure honestly.

    Returns the process exit code so a caller can tell a real send from a
    silent no-op -- "sent" must never be printed when nothing was sent.
    """
    try:
        proc = subprocess.run(
            ["hermes", "send", "--to", "telegram", text],
            capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        print(f"  send error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"  send failed ({proc.returncode}): "
              f"{(proc.stderr or proc.stdout).strip()[:200]}", file=sys.stderr)
    return proc.returncode


def run(*, apply: bool = True, do_send: bool = True) -> int:
    today = date.today().isoformat()
    started = datetime.now()

    # 1. Lay the day out. `apply=False` makes this a pure preview, so --dry-run
    #    cannot leave half-scheduled rows behind.
    plan = scheduler.build_day(USER_ID, day=today, apply=apply)
    planned = plan.get("planned") or []
    skipped = plan.get("skipped") or []

    print(f"manager: {USER_ID} {today}")
    print(f"  planned: {len(planned)}  skipped: {len(skipped)}  "
          f"apply={apply}")

    # 2. The brief is built AFTER the plan so it describes the day that now
    #    exists rather than the day that existed before planning.
    try:
        brief = daily_brief.build_brief()
    except Exception as exc:  # noqa: BLE001
        brief = f"Brief unavailable: {type(exc).__name__}: {exc}"
        print(f"  brief error: {brief}", file=sys.stderr)

    print(brief)

    if do_send:
        # One message, not two: the plan and the brief are the same
        # announcement, and two notifications a minute apart reads as a bug.
        rc = send(brief)
        print(f"  sent: {'yes' if rc == 0 else 'NO (rc=' + str(rc) + ')'}")
        if rc != 0:
            return rc

    print(f"  done in {(datetime.now() - started).total_seconds():.1f}s")
    return 0


def main() -> int:
    if "--snapshot" in sys.argv:
        print(json.dumps(snapshot(), indent=2, default=str))
        return 0
    dry = "--dry-run" in sys.argv
    no_send = "--no-send" in sys.argv or dry
    return run(apply=not dry, do_send=not no_send)


if __name__ == "__main__":
    sys.exit(main())
