"""Goal + task management routes: many goals, Hermes-managed tasks, quick actions.

These pin the behaviour the apps and widgets depend on:
  * many goals, each with its own task checklist
  * completing a blocker unlocks its dependent task (the product's core promise)
  * one-tap done/skip for the home-screen widget
  * deleting a goal does not leave orphaned tasks behind

Run: python3 test_goals.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Hermetic: never touch a live database or enforce auth.
for _k in ("DATABASE_URL", "SUPABASE_DB_URL", "SUPABASE_URL",
           "SUPABASE_SERVICE_KEY", "VECTOR_API_KEY"):
    os.environ.pop(_k, None)
os.environ["VECTOR_DB"] = os.path.join(tempfile.mkdtemp(), "goals.db")

import api  # noqa: E402
import store  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f"  [{extra}]" if extra else ""))


def call(method, path, body=None, user="josh"):
    status, _h, payload = api.route(method, path, body or {}, user)
    return status, json.loads(payload)


def main() -> int:
    store.reset_connections()

    # --- two independent goals, seeded directly so no LLM is needed ---------
    g1 = store.db_request("POST", "goals",
                          body={"user_id": "josh", "title": "Goal A"})[0]
    g2 = store.db_request("POST", "goals",
                          body={"user_id": "josh", "title": "Goal B"})[0]
    t1 = store.db_request("POST", "tasks", body={
        "user_id": "josh", "goal_id": g1["id"], "title": "A step 1",
        "minutes": 10})[0]
    t2 = store.db_request("POST", "tasks", body={
        "user_id": "josh", "goal_id": g1["id"], "title": "A step 2",
        "minutes": 20, "blocked_by": t1["id"]})[0]

    print("\nmany goals, one task list each")
    s, _ = call("POST", "/goals", {"title": "  "})
    check("blank goal title rejected", s == 400)
    s, b = call("POST", "/tasks", {"goal_id": g2["id"], "title": "B step 1",
                                   "minutes": 15})
    check("task can be added to an existing goal", s == 201, str(s))
    check("hand-added task is attributed to the user, not the AI",
          b.get("source") == "user", str(b.get("source")))
    s, _ = call("POST", "/tasks", {"goal_id": g2["id"]})
    check("blank task title rejected", s == 400)

    s, b = call("GET", f"/goals/{g1['id']}/tasks")
    check("goal task list returns 200", s == 200, str(s))
    check("only this goal's tasks are returned", b["total"] == 2,
          str(b["total"]))
    s, b2 = call("GET", f"/goals/{g2['id']}/tasks")
    check("goals do not leak into each other",
          [t["title"] for t in b2["tasks"]] == ["B step 1"],
          str([t["title"] for t in b2["tasks"]]))

    print("\ndependency state is reported per task")
    by_title = {t["title"]: t for t in b["tasks"]}
    check("unblocked task is startable", by_title["A step 1"]["startable"] is True)
    check("blocked task is NOT startable", by_title["A step 2"]["startable"] is False)
    check("blocked task names its blocker",
          by_title["A step 2"]["blocked_by_title"] == "A step 1",
          str(by_title["A step 2"]["blocked_by_title"]))

    print("\nquick actions (one tap from the widget)")
    s, _ = call("POST", f"/tasks/{t1['id']}/done")
    check("mark done returns 200", s == 200, str(s))
    s, b = call("GET", f"/goals/{g1['id']}/tasks")
    by_title = {t["title"]: t for t in b["tasks"]}
    check("finished task reports status done",
          by_title["A step 1"]["status"] == "done")
    check("finishing a task unlocks its dependent",
          by_title["A step 2"]["startable"] is True,
          str(by_title["A step 2"]["startable"]))
    check("done task is no longer startable",
          by_title["A step 1"]["startable"] is False)
    check("progress percentage updates", b["pct_done"] == 50, str(b["pct_done"]))

    s, _ = call("POST", f"/tasks/{t2['id']}/skip")
    check("skip returns 200", s == 200, str(s))
    s, b = call("GET", f"/goals/{g1['id']}/tasks")
    check("skipped task drops out of startable",
          all(t["startable"] is False for t in b["tasks"]))

    print("\na quick action on another user's task does nothing")
    other = store.db_request("POST", "tasks", body={
        "user_id": "someone-else", "title": "Not yours", "minutes": 5})[0]
    call("POST", f"/tasks/{other['id']}/done")
    row = store.db_request("GET", "tasks", params={"id": f"eq.{other['id']}"})
    check("cross-user quick action is scoped out",
          row and row[0]["status"] == "todo", str(row))

    print("\ngoal edit + delete")
    s, _ = call("PATCH", f"/goals/{g1['id']}", {"title": "A renamed"})
    check("goal can be renamed", s == 200, str(s))
    s, _ = call("PATCH", f"/goals/{g1['id']}", {})
    check("empty goal patch rejected", s == 400, str(s))

    before = len(store.db_request("GET", "tasks",
                                  params={"user_id": "eq.josh"}))
    s, _ = call("DELETE", f"/goals/{g1['id']}")
    after = len(store.db_request("GET", "tasks",
                                 params={"user_id": "eq.josh"}))
    check("deleting a goal returns 200", s == 200, str(s))
    check("its tasks are removed too (no orphans)", after == before - 2,
          f"{before} -> {after}")

    s, b = call("GET", "/tasks/startable")
    check("startable route still works (widgets depend on it)", s == 200,
          str(s))
    s, b = call("GET", "/goals")
    check("goal list still works", s == 200 and isinstance(b, list), str(s))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
