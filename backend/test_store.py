"""Tests for the local store.

Run against a throwaway SQLite file: no network, no Supabase, no LLM.

The single most important behaviour here is `startable_tasks`: if it ever
returns a task whose blocker is unfinished, the app shows work the user
cannot actually do, which is the exact failure the product exists to fix.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

# Hermetic: these tests must run on a throwaway SQLite file. If a
# DATABASE_URL is present (e.g. the Supabase DSN in backend/.env) the
# store would connect to the LIVE cloud database and the tests would
# write there. Clear it so the suite always targets a temp file.
for _k in (
    "DATABASE_URL", "SUPABASE_DB_URL", "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY", "VECTOR_API_KEY",
):
    os.environ.pop(_k, None)
os.environ["VECTOR_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def main() -> int:
    store.init_db()
    uid = str(uuid.uuid4())

    print("insert / read")
    g = store.db_request("POST", "goals", body={
        "user_id": uid, "title": "Learn quant finance"})
    check("goal insert returns a row", isinstance(g, list) and len(g) == 1)
    gid = g[0]["id"]
    check("goal id is a uuid", len(gid) == 36)
    check("default status applied", g[0]["status"] == "active")

    print("\nblocking chain")
    t1 = store.db_request("POST", "tasks", body={
        "user_id": uid, "goal_id": gid, "title": "Step one",
        "minutes": 20, "priority": 2})[0]
    t2 = store.db_request("POST", "tasks", body={
        "user_id": uid, "goal_id": gid, "title": "Step two",
        "minutes": 45, "blocked_by": t1["id"], "priority": 3})[0]
    t3 = store.db_request("POST", "tasks", body={
        "user_id": uid, "goal_id": gid, "title": "Step three",
        "minutes": 30, "blocked_by": t2["id"], "priority": 3})[0]

    startable = store.db_request("GET", "startable_tasks",
                                 params={"user_id": f"eq.{uid}"})
    titles = [r["title"] for r in startable]
    check("only the unblocked task is startable", titles == ["Step one"],
          f"got {titles}")
    check("blocked task is NOT startable", "Step two" not in titles)
    check("transitively blocked task is NOT startable", "Step three" not in titles)

    print("\nunblocking")
    store.db_request("PATCH", "tasks", params={"id": f"eq.{t1['id']}"},
                     body={"status": "done"})
    titles = [r["title"] for r in store.db_request(
        "GET", "startable_tasks", params={"user_id": f"eq.{uid}"})]
    check("finishing the blocker releases the next task",
          titles == ["Step two"], f"got {titles}")
    check("still does not release the doubly-blocked task",
          "Step three" not in titles)

    store.db_request("PATCH", "tasks", params={"id": f"eq.{t2['id']}"},
                     body={"status": "done"})
    titles = [r["title"] for r in store.db_request(
        "GET", "startable_tasks", params={"user_id": f"eq.{uid}"})]
    check("chain releases one step at a time", titles == ["Step three"],
          f"got {titles}")

    print("\nskipped tasks are not startable")
    store.db_request("PATCH", "tasks", params={"id": f"eq.{t3['id']}"},
                     body={"status": "skipped"})
    titles = [r["title"] for r in store.db_request(
        "GET", "startable_tasks", params={"user_id": f"eq.{uid}"})]
    check("skipped task drops out", titles == [], f"got {titles}")

    print("\nuser isolation")
    other = str(uuid.uuid4())
    got = store.db_request("GET", "goals", params={"user_id": f"eq.{other}"})
    check("another user sees nothing", got == [], f"got {got}")
    got = store.db_request("GET", "startable_tasks",
                           params={"user_id": f"eq.{other}"})
    check("another user has no startable tasks", got == [])

    print("\nordering and limits")
    u2 = str(uuid.uuid4())
    for title, prio, mins in [("c", 3, 60), ("a", 1, 90), ("b", 2, 10)]:
        store.db_request("POST", "tasks", body={
            "user_id": u2, "title": title, "priority": prio, "minutes": mins})
    rows = store.db_request("GET", "startable_tasks", params={"user_id": f"eq.{u2}"})
    check("priority ascending", [r["title"] for r in rows] == ["a", "b", "c"],
          f"got {[r['title'] for r in rows]}")

    rows = store.db_request("GET", "tasks",
                            params={"user_id": f"eq.{u2}", "limit": "2",
                                    "order": "minutes.asc"})
    check("limit honoured", len(rows) == 2, f"got {len(rows)}")
    check("order by minutes asc", rows[0]["title"] == "b", f"got {rows[0]['title']}")

    print("\nselect projection")
    rows = store.db_request("GET", "tasks", params={
        "user_id": f"eq.{u2}", "select": "id,title", "limit": "1"})
    check("select returns only asked columns", set(rows[0]) == {"id", "title"},
          f"got {sorted(rows[0])}")

    print("\nfinance")
    fs = store.db_request("POST", "finance_settings", body={
        "user_id": uid, "currency": "IDR", "balance": 3500000,
        "daily_budget": 116000})
    check("finance_settings insert (no id column) works",
          isinstance(fs, list) and len(fs) == 1, f"got {fs}")
    got = store.db_request("GET", "finance_settings",
                           params={"user_id": f"eq.{uid}", "limit": "1"})
    check("finance_settings reads back", len(got) == 1 and got[0]["balance"] == 3500000)

    store.db_request("POST", "expenses", body={
        "user_id": uid, "amount": 50000, "note": "groceries"})
    store.db_request("POST", "expenses", body={
        "user_id": uid, "amount": 25000, "note": "transport"})
    ex = store.db_request("GET", "expenses", params={"user_id": f"eq.{uid}"})
    check("expenses stored", len(ex) == 2, f"got {len(ex)}")
    check("spent_on defaults to today",
          all(e["spent_on"] for e in ex))

    print("\nunknown column rejected")
    try:
        store.db_request("POST", "tasks", body={
            "user_id": uid, "title": "x", "evil_column": 1})
        check("unknown column raises", False)
    except RuntimeError as e:
        check("unknown column raises", "unknown_column" in str(e), str(e))

    print("\nviews")
    gp = store.db_request("GET", "goal_progress", params={"user_id": f"eq.{uid}"})
    check("goal_progress returns the goal", len(gp) == 1, f"got {len(gp)}")
    check("goal_progress counts tasks", gp[0]["total_tasks"] == 3,
          f"got {gp[0]['total_tasks']}")
    check("goal_progress counts done", gp[0]["done_tasks"] == 2,
          f"got {gp[0]['done_tasks']}")
    check("pct_done is 67", gp[0]["pct_done"] == 67, f"got {gp[0]['pct_done']}")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
