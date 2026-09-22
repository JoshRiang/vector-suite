"""Tests for the daily brief.

The brief is the proactive half of the product, so its failure mode matters:
a brief that lists 12 pending items recreates the paralysis the product exists
to remove. These tests pin the contract -- exactly one named next action.

Runs against a throwaway DB with the API's own handlers, no network.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

os.environ["VECTOR_DB"] = os.path.join(tempfile.mkdtemp(), "brief.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api  # noqa: E402
import daily_brief  # noqa: E402
import store  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


class FakeApi:
    """Serve the brief from the real handlers, without a socket."""

    def __init__(self, uid: str):
        self.uid = uid

    def __call__(self, path: str):
        if path == "/today":
            return api.today_plan(self.uid)
        if path == "/goals":
            return api.db_request("GET", "goal_progress",
                                  params={"user_id": f"eq.{self.uid}"})
        if path == "/productivity":
            return api.productivity(self.uid)
        raise AssertionError(f"unexpected path {path}")


def main() -> int:
    store.init_db()

    print("no goals")
    uid = str(uuid.uuid4())
    daily_brief.api_get = FakeApi(uid)
    out = daily_brief.build_brief()
    check("invites a goal when none exist", "No goals yet" in out, out)
    check("does not invent a start action", "Start here" not in out, out)

    print("\nwith a goal")
    uid = str(uuid.uuid4())
    d = api.create_goal_with_tasks(uid, "Ship the VECTOR apps", None, None)
    daily_brief.api_get = FakeApi(uid)
    out = daily_brief.build_brief()
    check("names exactly one start action", out.count("Start here") == 1, out)
    check("includes the first task title",
          d["tasks"][0]["title"][:30] in out, out)
    check("states a time estimate", "min" in out, out)
    check("shows goal progress", "%" in out, out)

    print("\nit never dumps the whole backlog")
    check("does not list every task title",
          sum(1 for t in d["tasks"] if t["title"][:30] in out) <= 2,
          f"{sum(1 for t in d['tasks'] if t['title'][:30] in out)} task titles shown")

    # The "ignore them" note is conditional: it only makes sense when more than
    # one task is actually startable. With a linear chain only one ever is.
    n_startable = len(api.startable_tasks(uid))
    check("mentions ignoring the rest only when there is more than one",
          ("ignore them" in out) == (n_startable > 1),
          f"startable={n_startable}, note_present={'ignore them' in out}")

    print("\nwith several independent startable tasks")
    uid2 = str(uuid.uuid4())
    api.create_goal_with_tasks(uid2, "Learn stochastic calculus", None, None)
    api.create_goal_with_tasks(uid2, "Write a research note on volatility", None, None)
    daily_brief.api_get = FakeApi(uid2)
    out2 = daily_brief.build_brief()
    n2 = len(api.startable_tasks(uid2))
    check("two goals give two startable tasks", n2 == 2, f"got {n2}")
    check("still names exactly ONE start action", out2.count("Start here") == 1, out2)
    check("explicitly tells the user to ignore the others",
          "ignore them" in out2, out2)
    check("does not print the second task's title as a heading",
          out2.count("*Start here:*") == 1, out2)

    print("\nafter completing the first task")
    api.route("PATCH", "/tasks", {"id": d["tasks"][0]["id"], "status": "done"}, uid)
    daily_brief.api_get = FakeApi(uid)
    out = daily_brief.build_brief()
    check("advances to the next task",
          d["tasks"][1]["title"][:30] in out, out)
    check("does not re-show the finished task",
          d["tasks"][0]["title"][:30] not in out, out)
    check("reports the completed count", "Done so far today" in out, out)

    print("\nwhen everything is done")
    for t in d["tasks"][1:]:
        api.route("PATCH", "/tasks", {"id": t["id"], "status": "done"}, uid)
    daily_brief.api_get = FakeApi(uid)
    out = daily_brief.build_brief()
    check("does not invent work when the plan is finished",
          "Start here" not in out, out)
    check("says nothing is startable", "Nothing startable" in out, out)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
