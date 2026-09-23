"""The command console must apply exactly what it says, and nothing else.

This is the highest-risk surface in the product: a natural-language instruction
becomes real writes to the user's calendar. So the model's output is treated as
UNTRUSTED input and validated here, and these tests pin the guard rails:

  * an unknown action is rejected, not executed
  * a date outside a sane window is refused, not written
  * a task id from another user cannot be touched
  * a partial failure is reported as a partial failure, never as success
  * a failed model call changes nothing and says so

The LLM itself is stubbed: a test that depends on a live model is flaky by
design, and what needs testing is the validation layer, not the model.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Hermetic before importing store/api.
for _k in ("DATABASE_URL", "SUPABASE_DB_URL", "SUPABASE_URL",
           "SUPABASE_SERVICE_KEY", "VECTOR_API_KEY"):
    os.environ.pop(_k, None)
os.environ["VECTOR_DB"] = os.path.join(tempfile.mkdtemp(), "cmd.db")

import api  # noqa: E402
import commands  # noqa: E402
import store  # noqa: E402

UID = "cmd-test-user"
OTHER = "someone-else"

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f"  [{extra}]" if extra else ""))


def stub_llm(payload):
    """Replace the model call with a fixed payload."""
    def _fake(instruction, context, timeout=90):
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload)
    commands._call_llm = _fake


def make_task(user=UID, title="existing task", when=None):
    row = store.db_request("POST", "tasks", body={
        "user_id": user, "title": title, "minutes": 30, "status": "todo",
        "priority": 3, "scheduled_at": when,
    })
    return row[0] if isinstance(row, list) else row


def tomorrow() -> str:
    return (datetime.now(api.LOCAL_TZ).date() + timedelta(days=1)).isoformat()


def main() -> int:
    store.reset_connections()
    store.init_db()

    # --- an unknown action must be refused -------------------------------
    stub_llm({"ops": [{"action": "drop_all_tables"}], "reply": "done!"})
    out = commands.handle(UID, "delete everything")
    check("unknown action is rejected",
          out["count"] == 0 and out["applied"][0]["error"] == "unknown_action",
          str(out.get("applied")))

    # --- create_task writes a real row -----------------------------------
    stub_llm({"ops": [{"action": "create_task", "title": "Gym",
                       "scheduled_at": f"{tomorrow()}T07:00:00",
                       "minutes": 60}],
              "reply": "Added Gym tomorrow 07:00"})
    out = commands.handle(UID, "add gym tomorrow 7am for an hour")
    check("create_task reports success", out["count"] == 1, str(out))
    rows = store.db_request("GET", "tasks", params={
        "user_id": f"eq.{UID}", "title": "eq.Gym"}) or []
    check("create_task actually wrote the row", len(rows) == 1, str(rows))
    if rows:
        check("scheduled_at stored", (rows[0].get("scheduled_at") or "").startswith(tomorrow()),
              str(rows[0].get("scheduled_at")))
        check("minutes stored", rows[0].get("minutes") == 60, str(rows[0].get("minutes")))
        check("source marked as user", rows[0].get("source") == "user",
              str(rows[0].get("source")))

    # --- a bad date must be refused, not written --------------------------
    before = len(store.db_request("GET", "tasks",
                                  params={"user_id": f"eq.{UID}"}) or [])
    stub_llm({"ops": [{"action": "create_task", "title": "Far future",
                       "scheduled_at": "2999-01-01T09:00:00"}], "reply": "ok"})
    out = commands.handle(UID, "schedule something in 2999")
    after = len(store.db_request("GET", "tasks",
                                 params={"user_id": f"eq.{UID}"}) or [])
    check("out-of-window date refused", out["count"] == 0, str(out.get("applied")))
    check("no row written for a bad date", after == before, f"{before}->{after}")

    # --- relative dates must never be stored literally --------------------
    stub_llm({"ops": [{"action": "create_task", "title": "Relative",
                       "scheduled_at": "tomorrow"}], "reply": "ok"})
    out = commands.handle(UID, "add relative tomorrow")
    check("a non-absolute date is refused",
          out["count"] == 0 and out["applied"][0]["error"] == "bad_scheduled_at",
          str(out.get("applied")))

    # --- another user's task cannot be touched ----------------------------
    theirs = make_task(user=OTHER, title="not yours")
    stub_llm({"ops": [{"action": "delete_task", "id": theirs["id"]}], "reply": "ok"})
    out = commands.handle(UID, "delete that task")
    still = store.db_request("GET", "tasks",
                             params={"id": f"eq.{theirs['id']}"}) or []
    check("cross-user delete refused",
          out["count"] == 0 and out["applied"][0]["error"] == "task_not_found",
          str(out.get("applied")))
    check("the other user's task still exists", len(still) == 1)

    # --- complete_task flips status and stamps completion ------------------
    mine = make_task(title="finish me")
    stub_llm({"ops": [{"action": "complete_task", "id": mine["id"]}],
              "reply": "Marked done"})
    out = commands.handle(UID, "mark finish me done")
    row = (store.db_request("GET", "tasks",
                            params={"id": f"eq.{mine['id']}"}) or [{}])[0]
    check("complete_task reports success", out["count"] == 1, str(out))
    check("status is done", row.get("status") == "done", str(row.get("status")))
    check("completed_at stamped", bool(row.get("completed_at")), str(row))

    # --- update_task moves an existing item, keeping its id ---------------
    stub_llm({"ops": [{"action": "update_task", "id": mine["id"],
                       "scheduled_at": f"{tomorrow()}T15:00:00",
                       "location": "Office"}], "reply": "Moved"})
    out = commands.handle(UID, "move finish me to tomorrow 3pm at the office")
    row = (store.db_request("GET", "tasks",
                            params={"id": f"eq.{mine['id']}"}) or [{}])[0]
    check("update_task reports success", out["count"] == 1, str(out))
    check("scheduled_at moved",
          (row.get("scheduled_at") or "").startswith(tomorrow()),
          str(row.get("scheduled_at")))
    check("location written", row.get("location") == "Office", str(row.get("location")))
    check("update did not create a second row",
          len(store.db_request("GET", "tasks",
                               params={"user_id": f"eq.{UID}",
                                       "title": "eq.finish me"}) or []) == 1)

    # --- a model failure changes nothing and says so ----------------------
    stub_llm(RuntimeError("no LLM configured"))
    out = commands.handle(UID, "add something")
    check("llm failure is reported", out["ok"] is False, str(out))
    check("llm failure applies nothing", out["applied"] == [], str(out))
    check("llm failure explains itself", "planner" in (out.get("reply") or "").lower(),
          str(out.get("reply")))

    # --- unparseable model output is handled ------------------------------
    stub_llm("I am not JSON at all, sorry.")
    out = commands.handle(UID, "do the thing")
    check("unparseable output is handled",
          out["ok"] is False and out["applied"] == [], str(out))

    # --- a partial failure must be reported as partial --------------------
    good = make_task(title="will work")
    stub_llm({"ops": [
        {"action": "complete_task", "id": good["id"]},
        {"action": "complete_task", "id": "does-not-exist"},
    ], "reply": "Both done"})
    out = commands.handle(UID, "complete two things")
    check("partial success counts one", out["count"] == 1, str(out))
    check("partial failure is flagged in the reply",
          "could not" in (out.get("reply") or "").lower(), str(out.get("reply")))

    # --- an instruction is always logged, even when it fails --------------
    hist = commands.history(UID)
    roles = [h.get("role") for h in hist]
    check("instruction logged", "user" in roles, str(roles[:6]))
    check("reply logged", "assistant" in roles, str(roles[:6]))

    # --- empty instruction is refused -------------------------------------
    out = commands.handle(UID, "   ")
    check("empty instruction refused", out["ok"] is False, str(out))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
