#!/usr/bin/env python3
"""Build the day, then message the plan to Telegram.

Runs in the morning: ranks the open work, writes the times, and sends a plan the
user can read on his phone. Re-runnable — the schedule is derived from the task
list each time, so a second run of the day replaces the first rather than
stacking a second copy on top.

Usage:
  daily_plan.py            # build today's plan and send it
  daily_plan.py --dry-run  # print the plan, change nothing, send nothing
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

# Load the project env so DATABASE_URL and the LLM key resolve the same way the
# service sees them.
for _cand in (os.path.join(HERE, "..", ".env"), os.path.join(HERE, ".env")):
    if os.path.exists(_cand):
        for _line in open(_cand, encoding="utf-8"):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

import scheduler  # noqa: E402

USER_ID = "josh"
WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def clock(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return iso[11:16] if len(iso) >= 16 else iso


def priority_mark(p: int) -> str:
    # 1 is the most important; the marker is for reading at a glance.
    return {1: "\U0001F534", 2: "\U0001F7E0", 3: "\U0001F7E1"}.get(p, "\u26AA")


def main() -> int:
    dry = "--dry-run" in sys.argv
    today = date.today()
    result = scheduler.build_day(USER_ID, day=today.isoformat(), apply=not dry)

    planned = result.get("planned") or []
    skipped = result.get("skipped") or []
    if not planned:
        print("nothing to schedule today")
        if not dry:
            _send("No open work to schedule today.")
        return 0

    lines = [
        f"*Today's plan — {WEEKDAY[today.weekday()]} {today.day} "
        f"{today.strftime('%b')}*",
        "",
    ]
    for item in planned:
        mark = priority_mark(int(item.get("priority") or 3))
        tag = " (fixed)" if item.get("fixed") else ""
        lines.append(
            f"`{clock(item['scheduled_at'])}` {mark} {item['title']}"
            f" — {item['minutes']}m{tag}")

    total = sum(int(i.get("minutes") or 0) for i in planned)
    lines.append("")
    lines.append(f"*{total // 60}h {total % 60}m planned* across "
                 f"{len(planned)} blocks")

    if skipped:
        no_time = [s for s in skipped if s.get("reason") == "no_time_left"]
        blocked = [s for s in skipped if s.get("reason") == "blocked"]
        lines.append("")
        if no_time:
            over = result.get("unscheduled_minutes") or 0
            lines.append(f"Didn't fit: {len(no_time)} task(s), "
                         f"{over // 60}h {over % 60}m — "
                         "reply `move to tomorrow` if you want them shifted.")
        if blocked:
            lines.append(f"Blocked: {len(blocked)} task(s) waiting on earlier "
                         "work.")

    text = "\n".join(lines)
    print(text)
    if not dry:
        _send(text)
    return 0


def _send(text: str) -> None:
    """Send via the hermes CLI, reporting failure instead of swallowing it."""
    try:
        proc = subprocess.run(
            ["hermes", "send", "--to", "telegram", text],
            capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            print(f"  send failed ({proc.returncode}): "
                  f"{(proc.stderr or proc.stdout).strip()[:200]}",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"  send error: {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
