#!/usr/bin/env python3
"""Smoke-test the goal/task routes against the LIVE API.

This is the integration check the unit tests cannot give: the real uvicorn
process, the real store, real HTTP.

IMPORTANT: `POST /goals` also runs LLM decomposition, so a freshly created goal
already has an AI-written plan of unpredictable length. This script therefore
asserts on BEHAVIOUR (exactly one startable task, completing unblocks the next,
delete cascades) rather than exact task counts, which would make it flaky.

Usage:
    set -a; . ../.env; set +a; python3 smoke_live.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

API = os.environ.get("VECTOR_API", "http://127.0.0.1:8790")

# Read the key the same way the service does.
key = os.environ.get("VECTOR_API_KEY", "")
if not key:
    root_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(root_env) as fh:
            for line in fh:
                if line.startswith("VECTOR_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    except OSError:
        pass
if not key:
    sys.exit("no VECTOR_API_KEY found")

UID = "smoke-" + str(uuid.uuid4())[:8]
# Identity travels in a header, not the body -- the server ignores a body
# user_id and answers 401 missing_user. Getting this wrong made every call
# fail on the first run of this script.
H = {"X-Api-Key": key, "X-User-Id": UID, "Content-Type": "application/json"}
OK = (200, 201)


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method, headers=H,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {str(detail)[:220]}")


print(f"live API smoke test  (user {UID})")

print("\n1. many goals, not one")
st, resp1 = call("POST", "/goals", {"title": "Ship the VECTOR apps"})
check("create goal 1", st in OK, f"{st} {resp1}")
g1 = resp1.get("goal", {}) if isinstance(resp1, dict) else {}
plan1 = resp1.get("tasks", []) if isinstance(resp1, dict) else []
check("goal 1 came back with an AI plan", len(plan1) >= 1, str(len(plan1)))
check("the plan is not degraded", resp1.get("degraded") is False, str(resp1)[:150])

st, resp2 = call("POST", "/goals", {"title": "Build a quant track record"})
check("create goal 2", st in OK, f"{st} {resp2}")
g2 = resp2.get("goal", {}) if isinstance(resp2, dict) else {}

st, goals = call("GET", "/goals")
check("both goals listed separately",
      st in OK and isinstance(goals, list) and len(goals) == 2,
      f"{st} {len(goals) if isinstance(goals, list) else goals}")

print("\n2. the plan is a real dependency chain")
st, detail = call("GET", f"/goals/{g1['id']}/tasks")
check("checklist fetch", st in OK, str(st))
tasks = detail["tasks"]
check("every task has a title", all(t["title"] for t in tasks), str(tasks)[:150])
check("every task has a time estimate",
      all(isinstance(t.get("minutes"), int) for t in tasks), str(tasks)[:150])
startable = [t for t in tasks if t["startable"]]
# NOT "exactly one": plan shape varies by model run, and a plan whose first
# steps are independent legitimately has several roots. The invariant is that
# startable rows are genuinely unblocked.
by_id = {t["id"]: t for t in tasks}
check("at least one task can be started", len(startable) >= 1,
      f"{len(startable)} startable")
check("far fewer startable than total (not the whole backlog dumped)",
      len(startable) < len(tasks), f"{len(startable)} of {len(tasks)}")
misreported = [
    t["title"][:40] for t in tasks
    if t["startable"] and t.get("blocked_by")
    and by_id[t["blocked_by"]]["status"] != "done"
]
check("no task claims to be startable while its blocker is unfinished",
      misreported == [], str(misreported))
blocked = [t for t in tasks if not t["startable"]]
if blocked:
    check("blocked tasks name what they wait on",
          all(t.get("blocked_by_title") for t in blocked), str(blocked)[:200])
check("progress starts at 0%", detail["pct_done"] == 0, str(detail["pct_done"]))

print("\n3. Hermes can add tasks under a goal")
# Added to goal 2 on purpose: an unblocked task added to goal 1 would be
# startable straight away and break the one-startable chain assertions below.
st, mine = call("POST", "/tasks", {
    "goal_id": g2["id"], "title": "Write the release checklist",
    "minutes": 15, "priority": 2, "source": "ai"})
check("add a task to goal 2", st in OK, f"{st} {mine}")
check("the new task carries the goal id",
      isinstance(mine, dict) and mine.get("goal_id") == g2["id"], str(mine)[:150])

print("\n4. completing a task unblocks the next one")
first = startable[0]
st, _ = call("PATCH", "/tasks", {"id": first["id"], "status": "done"})
check("mark the startable task done", st in OK, str(st))
st, detail2 = call("GET", f"/goals/{g1['id']}/tasks")
check("progress moved above 0%", detail2["pct_done"] > 0, str(detail2["pct_done"]))
now_startable = [t for t in detail2["tasks"] if t["startable"]]
check("work advanced: something else is startable now",
      len(now_startable) >= 1, f"{len(now_startable)} startable")
check("the finished task is no longer offered as startable",
      all(t["id"] != first["id"] for t in now_startable),
      str(now_startable)[:150])
by_id2 = {t["id"]: t for t in detail2["tasks"]}
check("every newly startable task is genuinely unblocked",
      all(not t.get("blocked_by")
          or by_id2[t["blocked_by"]]["status"] == "done"
          for t in now_startable), str(now_startable)[:200])
check("the finished task reports as done",
      [t for t in detail2["tasks"] if t["id"] == first["id"]][0]["status"] == "done",
      str(detail2)[:150])

print("\n5. reopen restores the blocked state (undo works)")
st, _ = call("PATCH", "/tasks", {"id": first["id"], "status": "todo"})
check("reopen the task", st in OK, str(st))
st, detail3 = call("GET", f"/goals/{g1['id']}/tasks")
check("back to 0%", detail3["pct_done"] == 0, str(detail3["pct_done"]))
if now_startable:
    reopened_ids = {t["id"] for t in now_startable}
    still = [t["title"][:40] for t in detail3["tasks"]
             if t["id"] in reopened_ids and t["startable"]]
    check("everything that was unblocked is blocked again",
          still == [], str(still))

print("\n6. /today offers a starting point, not the whole backlog")
st, today = call("GET", "/today")
check("/today responds", st in OK, f"{st} {str(today)[:150]}")
if isinstance(today, dict):
    tl = today.get("startable", [])
    check("today lists something to start", len(tl) >= 1, f"{len(tl)} startable")
    st, g1detail = call("GET", f"/goals/{g1['id']}/tasks")
    g1_total = len(g1detail["tasks"])
    check("today shows far less than the full backlog",
          len(tl) < g1_total, f"{len(tl)} shown vs {g1_total} in goal 1")
    check("today includes goal 1's startable task",
          any(t["id"] == first["id"] for t in tl), str(tl)[:200])

print("\n7. deleting a goal cascades to its tasks")
st, _ = call("DELETE", f"/goals/{g2['id']}")
check("delete goal 2", st in OK, str(st))
st, goals2 = call("GET", "/goals")
check("one goal left", isinstance(goals2, list) and len(goals2) == 1,
      str(goals2)[:150])
# GET /tasks is not a route, so orphans are checked through the goal view:
# if the cascade worked, the deleted goal now has no tasks.
st, orphan = call("GET", f"/goals/{g2['id']}/tasks")
check("its tasks are gone too (no orphans)",
      st in OK and orphan.get("tasks") == [], str(orphan)[:200])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
