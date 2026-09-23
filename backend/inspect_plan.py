#!/usr/bin/env python3
"""Inspect a real decomposed plan as a dependency graph.

WHY: the smoke test showed one completed task unblocking TWO others. That is
either a legitimate DAG (two tasks sharing one prerequisite) or a bug in the
startable calculation. This prints the raw blocked_by edges so the answer comes
from data, not from guessing.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

API = os.environ.get("VECTOR_API", "http://127.0.0.1:8790")
key = os.environ.get("VECTOR_API_KEY", "")
if not key:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) as fh:
        for line in fh:
            if line.startswith("VECTOR_API_KEY="):
                key = line.split("=", 1)[1].strip()

UID = "graph-" + str(uuid.uuid4())[:8]
H = {"X-Api-Key": key, "X-User-Id": UID, "Content-Type": "application/json"}


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method, headers=H,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


st, resp = call("POST", "/goals", {"title": "Ship the VECTOR apps"})
gid = resp["goal"]["id"]
print(f"goal {gid}\n")

st, d = call("GET", f"/goals/{gid}/tasks")
tasks = d["tasks"]
by_id = {t["id"]: t for t in tasks}

print("EDGES (blocked_by -> task)")
edges = 0
for t in tasks:
    b = t.get("blocked_by")
    if b:
        edges += 1
        print(f"  {by_id[b]['title'][:45]:47s} -> {t['title'][:45]}")
    else:
        print(f"  {'(no blocker)':47s} -> {t['title'][:45]}")
print(f"\ntasks={len(tasks)} edges={edges} "
      f"roots={sum(1 for t in tasks if not t.get('blocked_by'))}")

print("\nchildren per task (how many each one unblocks)")
from collections import Counter
kids = Counter(t["blocked_by"] for t in tasks if t.get("blocked_by"))
for tid, n in kids.items():
    print(f"  {n} child(ren): {by_id[tid]['title'][:50]}")

print(f"\nstartable now: {sum(1 for t in tasks if t['startable'])}")
print("  " + "\n  ".join(t["title"][:60] for t in tasks if t["startable"]))

# Complete the current startable task(s) and see what opens up.
first = [t for t in tasks if t["startable"]][0]
st, _ = call("PATCH", "/tasks", {"id": first["id"], "status": "done"})
st, d2 = call("GET", f"/goals/{gid}/tasks")
after = [t for t in d2["tasks"] if t["startable"]]
print(f"\nafter completing '{first['title'][:50]}':")
print(f"  startable now: {len(after)}")
for t in after:
    print(f"    - {t['title'][:60]}")

# The honest question: is every one of those genuinely unblocked?
print("\nverification of each newly-startable task:")
for t in after:
    b = t.get("blocked_by")
    if not b:
        print(f"  OK   '{t['title'][:40]}' has no blocker at all")
    else:
        bstat = [x for x in d2["tasks"] if x["id"] == b][0]["status"]
        verdict = "OK  " if bstat == "done" else "BUG "
        print(f"  {verdict} '{t['title'][:40]}' blocked_by={bstat}")
