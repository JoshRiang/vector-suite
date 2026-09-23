"""End-to-end: a real natural-language instruction -> real calendar changes.

Runs against the REAL model and the REAL store, but under a throwaway user id
so the user's own calendar is never touched. Everything created here is deleted
at the end.

This is the test that matters: the unit suite stubs the model, so it proves the
validation layer but not that the model actually produces usable operations.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load the same env the service uses, so the real LLM config is picked up.
for env in ("/home/josh/vector_suite/.env",
            "/home/josh/vector_suite/backend/.env"):
    if os.path.exists(env):
        for line in open(env):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# Real LLM, but a throwaway LOCAL database.
#
# Using the production Postgres here would both risk the user's real calendar
# and time out applying the schema through the pooler. A temp SQLite file gives
# the same store code path while touching nothing real.
os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)
import tempfile  # noqa: E402
os.environ["VECTOR_DB"] = os.path.join(tempfile.mkdtemp(), "e2e.db")

import api  # noqa: E402
import commands  # noqa: E402
import store  # noqa: E402

UID = "e2e-command-test"


def cleanup():
    for t in store.db_request("GET", "tasks",
                              params={"user_id": f"eq.{UID}"}) or []:
        store.db_request("DELETE", "tasks", params={"id": f"eq.{t['id']}"})
    for g in store.db_request("GET", "goals",
                              params={"user_id": f"eq.{UID}"}) or []:
        store.db_request("DELETE", "goals", params={"id": f"eq.{g['id']}"})
    for c in store.db_request("GET", "chat_messages",
                              params={"user_id": f"eq.{UID}"}) or []:
        store.db_request("DELETE", "chat_messages",
                         params={"id": f"eq.{c['id']}"})


def tasks():
    return store.db_request("GET", "tasks",
                            params={"user_id": f"eq.{UID}"}) or []


def run(instruction: str):
    out = commands.handle(UID, instruction)
    print(f"\n  IN : {instruction}")
    print(f"  OUT: {out.get('reply')}")
    for a in out.get("applied", []):
        print(f"       {a.get('action')}: ok={a.get('ok')} "
              f"{a.get('error') or a.get('title') or a.get('id') or ''}")
    return out


def main() -> int:
    fails: list[str] = []
    cleanup()

    # 1. a plain create with a relative date
    out = run("add a dentist appointment tomorrow at 2pm for 45 minutes")
    t = tasks()
    if not t:
        fails.append("create produced no task")
    else:
        row = t[0]
        sched = (row.get("scheduled_at") or "")
        expected_day = api._days_ahead_local(1)
        if not sched.startswith(expected_day):
            fails.append(f"expected {expected_day}, stored {sched}")
        if row.get("minutes") != 45:
            fails.append(f"expected 45 min, stored {row.get('minutes')}")
        print(f"     -> stored: {row.get('title')} @ {sched} "
              f"({row.get('minutes')}min)")

    # 2. a question must not mutate anything
    before = len(tasks())
    run("what do I have tomorrow?")
    after = len(tasks())
    if after != before:
        fails.append(f"a question changed data: {before} -> {after}")
    else:
        print(f"     -> no mutation ({before} tasks unchanged)")

    # 3. a follow-up that references the existing item
    if t:
        target = t[0]
        run("move the dentist appointment to 4pm")
        row = (store.db_request("GET", "tasks",
                                params={"id": f"eq.{target['id']}"}) or [{}])[0]
        sched = row.get("scheduled_at") or ""
        print(f"     -> now {sched}")
        if not sched.endswith("16:00:00"):
            fails.append(f"move did not apply: {sched}")
        if len(tasks()) != before:
            fails.append("move created a duplicate instead of updating")

    # 4. completing it
    if t:
        run("mark the dentist appointment as done")
        row = (store.db_request("GET", "tasks",
                                params={"id": f"eq.{t[0]['id']}"}) or [{}])[0]
        if row.get("status") != "done":
            fails.append(f"complete did not apply: {row.get('status')}")
        else:
            print("     -> status=done")

    cleanup()
    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("REAL-LLM COMMAND PATH VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
