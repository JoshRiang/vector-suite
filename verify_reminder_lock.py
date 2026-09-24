#!/usr/bin/env python3
"""Prove the reminder lock prevents a duplicate delivery under concurrency.

The cron ticks every minute; a manual run or a slow delivery can overlap it.
Before the lock, two runs each read the sent-ledger before either wrote to it,
so ONE reminder produced TWO ledger entries 0.36s apart - i.e. the user would
get the same reminder twice. This starts several workers at once on a reminder
that is due right now and asserts exactly one delivery.

Exit 0 = the lock holds. Exit 1 = duplicates got through.
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

BACKEND = "/home/josh/vector_suite/backend"
DATA = "/home/josh/vector_suite/data"
LEDGER = os.path.join(DATA, "reminders_sent.jsonl")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import store  # noqa: E402

WORKERS = 6


def ledger_lines() -> int:
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def main() -> int:
    # Clean slate so a stale lock or old entry cannot mask the result.
    lock = os.path.join(DATA, ".remind.lock")
    if os.path.exists(lock):
        os.remove(lock)

    # The reminder window is ONE minute wide, starting at the top of the minute
    # (fire = scheduled_at - lead, truncated to the minute). Starting the test
    # mid-minute leaves almost no margin, so a run that crosses the boundary
    # silently fires nothing and the lock goes untested. Wait for a fresh minute.
    now = datetime.now()
    if now.second > 5:
        wait = 60 - now.second + 1
        print(f"aligning to the start of the next minute ({wait}s)...")
        time.sleep(wait)

    # A P1 task needs a 60-minute lead, so this fires in the current minute.
    sched = (datetime.now() + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M")
    created = store.db_request("POST", "tasks", body={
        "user_id": "josh", "title": "CONCURRENCY TEST", "priority": 1,
        "status": "todo", "scheduled_at": sched, "minutes": 15,
    })
    task = created[0] if isinstance(created, list) else created
    tid = str(task["id"])
    print(f"task {tid} scheduled {sched} (due now, P1 lead=60m)")

    # Confirm the reminder really is due BEFORE spawning workers, otherwise a
    # "0 deliveries" result is ambiguous between a broken lock and a dead test.
    import scheduler  # noqa: E402
    due = scheduler.due_reminders("josh")
    if not any(str(d["id"]) == tid for d in due):
        print("ABORT: the probe reminder is not due; the test would prove nothing")
        store.db_request("DELETE", "tasks", params={"id": f"eq.{tid}"})
        return 2
    print(f"  due confirmed ({len(due)} due now)")

    before = ledger_lines()
    procs = [subprocess.Popen(
        [sys.executable, "remind.py"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(WORKERS)]
    outputs = [p.communicate(timeout=180)[0] for p in procs]
    after = ledger_lines()
    new = after - before

    delivered = sum("1 delivered" in o for o in outputs)
    print(f"\n{WORKERS} concurrent workers")
    print(f"  ledger entries added : {new}")
    print(f"  runs reporting a send: {delivered}")
    for i, o in enumerate(outputs, 1):
        last = [ln for ln in o.strip().splitlines() if ln.strip()]
        print(f"  worker {i}: {last[-1] if last else '(no output)'}")

    # Clean up the probe task so it never reaches the user's real list.
    store.db_request("DELETE", "tasks", params={"id": f"eq.{tid}"})
    rows = store.db_request("GET", "tasks", params={"id": f"eq.{tid}"})
    left = len(rows) if isinstance(rows, list) else 0
    print(f"  probe task removed   : {left == 0}")

    if new == 1 and delivered == 1 and left == 0:
        print("\nLOCK VERIFIED: exactly one delivery for one due reminder")
        return 0
    print(f"\nFAILED: {new} ledger entries, {delivered} senders (want 1 and 1)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
