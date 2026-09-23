#!/usr/bin/env python3
"""Exercise the EXACT requests the widgets make, on a throwaway user.

The widgets do not use the generic API client -- they hit /today and the
bodyless quick-action verbs (/tasks/<id>/done, /reopen, /skip) over plain
HTTP. This checks those specific calls against the funnel URL the built APKs
actually point at, so a widget that cannot complete a task is caught here
rather than on the phone.

Uses a random user id: the real book must not be touched.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get(
    "VECTOR_API", "https://vector-server.tail53166f.ts.net")
key = os.environ.get("VECTOR_API_KEY", "")
if not key:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) as fh:
        for line in fh:
            if line.startswith("VECTOR_API_KEY="):
                key = line.split("=", 1)[1].strip()

UID = "widget-" + str(uuid.uuid4())[:8]
H = {"X-Api-Key": key, "X-User-Id": UID, "Content-Type": "application/json"}
PASS = FAIL = 0


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method, headers=H,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {str(detail)[:180]}")


print(f"widget request paths  (base {BASE}, user {UID})")

# The widget renders from /today and reads `startable`.
st, today = call("GET", "/today")
check("/today responds", st == 200, f"{st} {str(today)[:120]}")
check("has the `startable` array the widget parses",
      isinstance(today, dict) and "startable" in today, str(today)[:120])
check("has `done_today` for the completed count",
      isinstance(today, dict) and "done_today" in today, str(today)[:120])

# Seed a goal + tasks, then drive the widget's verbs.
st, resp = call("POST", "/goals", {"title": "Widget check"})
gid = resp["goal"]["id"]
st, d = call("GET", f"/goals/{gid}/tasks")
tasks = d["tasks"]
first = [t for t in tasks if t["startable"]][0]
print(f"\n  (seeded {len(tasks)} tasks; startable: {first['title'][:40]})")

# POST /tasks/<id>/done -- no body, exactly as the widget sends it.
st, r = call("POST", f"/tasks/{first['id']}/done")
check("POST /tasks/<id>/done works with no body", st == 200, f"{st} {r}")
st, d2 = call("GET", f"/goals/{gid}/tasks")
row = [t for t in d2["tasks"] if t["id"] == first["id"]][0]
check("the task is now done", row["status"] == "done", str(row)[:120])
check("completed_at is stamped (drives 'done today')",
      bool(row.get("completed_at")), str(row)[:120])
st, today2 = call("GET", "/today")
check("it shows up in done_today",
      any(t["id"] == first["id"] for t in today2["done_today"]),
      str(today2.get("done_today"))[:150])

# The checkbox toggles both ways.
st, r = call("POST", f"/tasks/{first['id']}/reopen")
check("POST /tasks/<id>/reopen works", st == 200, f"{st} {r}")
st, d3 = call("GET", f"/goals/{gid}/tasks")
row3 = [t for t in d3["tasks"] if t["id"] == first["id"]][0]
check("the task is back to todo", row3["status"] == "todo", str(row3)[:120])
check("completed_at was cleared (no phantom 'done today')",
      not row3.get("completed_at"), str(row3)[:120])

# Skip.
st, r = call("POST", f"/tasks/{first['id']}/skip")
check("POST /tasks/<id>/skip works", st == 200, f"{st} {r}")

# Clean up so the throwaway user leaves nothing behind.
st, _ = call("DELETE", f"/goals/{gid}")
check("throwaway goal deleted", st == 200, str(st))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
