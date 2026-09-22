#!/usr/bin/env python3
"""
Daily brief -- the proactive half of the product.

WHY THIS EXISTS
---------------
The apps are pull-based: the user has to open them. But the stated failure is
"I don't know where to start", which happens BEFORE the app gets opened. So the
system also has to reach out.

This runs on a schedule, asks the backend what is actually startable right now,
and sends exactly ONE concrete next action -- not a summary of everything
outstanding. A digest of 12 pending items is the paralysis the product exists
to remove; a single named action is not.

It also reports what got done yesterday, because visible progress is what makes
the next step feel worth taking.

Usage:
    python3 daily_brief.py            # print the brief
    python3 daily_brief.py --send     # print and deliver to Telegram
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = os.environ.get("VECTOR_API", "http://127.0.0.1:8790")
USER_ID = os.environ.get("VECTOR_USER", "josh")
TZ = timezone(timedelta(hours=7))          # WIB


def api_get(path: str) -> object:
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"X-User-Id": USER_ID, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.URLError as e:
        raise SystemExit(f"backend unreachable at {API}: {e}")


def build_brief() -> str:
    today = api_get("/today")
    goals = api_get("/goals")
    prod = api_get("/productivity")

    startable = today.get("startable") or []
    done = today.get("done_today") or []
    focus = today.get("focus_minutes") or 0
    streak = prod.get("current_streak") or 0

    now = datetime.now(TZ)
    lines = [f"*{now.strftime('%A, %d %b')}*"]

    # Yesterday's follow-through, so effort is visible.
    if done:
        lines.append(f"Done so far today: {len(done)} task"
                     f"{'s' if len(done) != 1 else ''}"
                     + (f" · {focus} min focused" if focus else ""))
    if streak > 1:
        lines.append(f"{streak}-day streak. Don't break it.")

    if not startable:
        if goals:
            lines.append("")
            lines.append("Nothing startable — every plan is either finished or "
                         "waiting on a task you marked done. Set a new goal.")
        else:
            lines.append("")
            lines.append("No goals yet. Tell me one outcome you want, and I'll "
                         "build the plan.")
        return "\n".join(lines)

    # ONE action. The whole point.
    top = startable[0]
    lines.append("")
    lines.append(f"*Start here:* {top['title']}")
    if top.get("why"):
        lines.append(f"_{top['why']}_")
    lines.append(f"{top.get('minutes', 30)} min")

    if len(startable) > 1:
        lines.append("")
        lines.append(f"({len(startable) - 1} more unlocked after this one — "
                     f"ignore them for now.)")

    # Goal-level progress, kept short.
    active = [g for g in goals if g.get("status") == "active"]
    if active:
        lines.append("")
        for g in active[:3]:
            lines.append(f"· {g['title'][:52]} — {g.get('pct_done', 0)}%")

    return "\n".join(lines)


def send(text: str) -> int:
    """Deliver via the Hermes gateway so it lands in the same chat."""
    try:
        r = subprocess.run(
            ["hermes", "send", "--to", "telegram", text],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            print(f"send failed: {r.stderr[:300]}", file=sys.stderr)
        return r.returncode
    except FileNotFoundError:
        print("hermes CLI not found on PATH", file=sys.stderr)
        return 127
    except subprocess.TimeoutExpired:
        print("send timed out", file=sys.stderr)
        return 124


def main() -> int:
    brief = build_brief()
    print(brief)
    if "--send" in sys.argv:
        return send(brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
