#!/usr/bin/env python3
"""Measure how many ROOTS (tasks with no blocker) real decompositions produce.

WHY: the smoke test asserted "exactly one startable task" and intermittently
saw two. The engine turned out to be correct -- a plan with two independent
first steps legitimately has two startable tasks. So the assertion was
measuring the LLM's plan SHAPE, not the engine's correctness.

This quantifies the shape so the tests and the UI can be written against
reality: if most plans are linear, "exactly one" is a safe expectation for the
widget; if many have several roots, the UI must handle a set.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from collections import Counter

API = os.environ.get("VECTOR_API", "http://127.0.0.1:8790")
key = os.environ.get("VECTOR_API_KEY", "")
if not key:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) as fh:
        for line in fh:
            if line.startswith("VECTOR_API_KEY="):
                key = line.split("=", 1)[1].strip()

TITLES = [
    "Ship the VECTOR apps",
    "Build a quant track record",
    "Get the portfolio risk dashboard live",
    "Learn stochastic calculus properly",
    "Write the RPL 911 final report",
]

H = {"X-Api-Key": key, "Content-Type": "application/json"}


def call(method, path, body=None, uid=None):
    h = dict(H)
    if uid:
        h["X-User-Id"] = uid
    req = urllib.request.Request(
        API + path, method=method, headers=h,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


roots_hist = Counter()
depths = []
print(f"{'goal':38s} {'tasks':>5s} {'roots':>5s} {'edges':>5s} {'degraded':>8s}")
for title in TITLES:
    uid = "shape-" + str(uuid.uuid4())[:8]
    st, resp = call("POST", "/goals", {"title": title}, uid=uid)
    if st not in (200, 201):
        print(f"{title[:38]:38s} FAILED {st} {resp}")
        continue
    gid = resp["goal"]["id"]
    st, d = call("GET", f"/goals/{gid}/tasks", uid=uid)
    tasks = d["tasks"]
    roots = [t for t in tasks if not t.get("blocked_by")]
    edges = [t for t in tasks if t.get("blocked_by")]
    roots_hist[len(roots)] += 1
    depths.append(len(tasks))
    print(f"{title[:38]:38s} {len(tasks):5d} {len(roots):5d} {len(edges):5d} "
          f"{str(resp.get('degraded')):>8s}")
    # Every startable task must genuinely be unblocked -- the real invariant.
    bad = []
    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        if t["startable"]:
            b = t.get("blocked_by")
            if b and by_id[b]["status"] != "done":
                bad.append(t["title"][:40])
    if bad:
        print(f"    BUG: startable but blocked: {bad}")

print(f"\nroot-count distribution: {dict(roots_hist)}")
print(f"plan sizes: {depths}")
print(f"linear (1 root) plans: {roots_hist[1]}/{len(depths)}")
