#!/usr/bin/env python3
"""Replicate the tasks app's _reload() in Python against the LIVE API.

The app cannot be run on this machine (no Flutter/Dart/Android SDK), so the
only way to prove that a task created by an instruction actually becomes visible
is to execute the same request sequence the app makes and apply the same
grouping rules to the responses.

This is a simulation of the CLIENT's logic, not of the data: every value below
comes from a real HTTP response.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

ROOT = "/home/josh/vector_suite"
ENV = os.path.join(ROOT, ".env")
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

BASE = "https://vector-server.tail53166f.ts.net"
KEY = os.environ["VECTOR_API_KEY"]
UID = "josh"
NO_LIST = "__none__"


def get(path: str):
    req = urllib.request.Request(
        BASE + path, headers={"X-Api-Key": KEY, "X-User-Id": UID})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def post(path: str, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"X-Api-Key": KEY, "X-User-Id": UID,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def main() -> int:
    print("STEP 1  create a task by instruction (no goal, no list)")
    # A unique title each run: the model correctly refuses to duplicate an
    # existing item, which would make this probe report a false failure.
    stamp = datetime.now().strftime("%H%M%S")
    title = f"probe appointment {stamp}"
    res = post("/command", {"instruction":
                            f"add a task called '{title}' tomorrow at 3pm for 30 minutes"})
    print("  reply:", res.get("reply"))
    created = [a for a in res.get("applied", []) if a.get("action") == "create_task"]
    if not created:
        print("  FAIL no create_task was applied:", json.dumps(res.get("applied"))[:200])
        return 1
    new_id = created[0].get("id", "")
    print("  new task id:", new_id[:8])

    print("\nSTEP 2  run the app's reload sequence")
    goals = get("/goals")
    print(f"  GET /goals -> {len(goals)} lists")

    by_goal: dict[str, list] = {}
    titles: dict[str, str] = {}
    for g in goals:
        gid = (g.get("goal_id") or g.get("id") or "")
        if not gid:
            continue
        titles[gid] = g.get("title") or "Untitled list"
        data = get(f"/goals/{gid}/tasks")
        tasks = data.get("tasks") if isinstance(data, dict) else []
        tasks = tasks if isinstance(tasks, list) else []
        for t in tasks:
            t["_goal_id"] = gid
        by_goal[gid] = tasks
    via_goals = sum(len(v) for v in by_goal.values())
    print(f"  tasks reachable through a goal: {via_goals}")

    found_via_goals = any(
        (t.get("id") or "") == new_id for v in by_goal.values() for t in v)
    print(f"  is the new task among them? {found_via_goals}")

    print("\nSTEP 3  the calendar fetch the app now also performs")
    now = datetime.now()
    start = (now - timedelta(days=60)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=120)).strftime("%Y-%m-%d")
    rng = get(f"/calendar/range?start={start}&end={end}")
    items = rng.get("items") or []
    print(f"  GET /calendar/range -> {len(items)} items")

    seen = {(t.get("id") or "") for v in by_goal.values() for t in v}
    dated = []
    for m in items:
        tid = m.get("id") or ""
        if not tid or tid in seen:
            continue
        seen.add(tid)
        gid = m.get("goal_id") or ""
        if gid and gid in titles:
            m["_goal_id"] = gid
            m["_goal_title"] = titles[gid]
        else:
            m["_goal_id"] = NO_LIST
            m["_goal_title"] = "No list"
        dated.append(m)
    if dated:
        by_goal[NO_LIST] = dated
        titles[NO_LIST] = "No list"
    print(f"  extra dated tasks added: {len(dated)}")

    found = any((t.get("id") or "") == new_id
                for t in by_goal.get(NO_LIST, []))
    print(f"\nSTEP 4  is the instruction-created task now VISIBLE? {found}")

    no_list_items = by_goal.get(NO_LIST, [])
    print(f"  'No list' group contains {len(no_list_items)} task(s):")
    for t in no_list_items[:5]:
        print(f"    - {t.get('title')}  @ {t.get('scheduled_at')}")

    total = sum(len(v) for v in by_goal.values())
    print(f"\n  total tasks the app would render: {total} "
          f"(was {via_goals} without the calendar fetch)")

    if not found:
        print("\nRESULT: FAIL — the task created by instruction is still invisible")
        return 1
    print("\nRESULT: PASS — an instruction-created task now appears in the app")
    return 0


if __name__ == "__main__":
    sys.exit(main())
