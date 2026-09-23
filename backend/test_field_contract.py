"""Contract test: every API field the Dart apps read must exist in the response.

WHY THIS EXISTS
---------------
The tasks app shipped broken while every existing check passed. `check_apps.py`
validates structure and `run_all_tests.py` validates the backend, but NOTHING
compared the field names the Dart code reads against the keys the API actually
returns. The app asked for `goal['id']` while `/goals` sends `goal_id`, so it
requested `/goals/null/tasks` -- which returns HTTP 200 with an empty list, not
an error. The app silently showed every goal with zero tasks.

A silent 200 is exactly the failure mode unit tests miss, so this suite parses
the Dart source for the keys it reads off each payload and asserts they are
present in the LIVE response. Add a key to the Dart and forget the API, or
rename an API field, and this fails loudly.

Run: python3 backend/test_field_contract.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("VECTOR_BASE_URL", "http://127.0.0.1:8790")
KEY = os.environ.get("VECTOR_API_KEY", "")
UID = "josh"

# Dart sources whose field reads must match the API.
APPS = {
    "vector-tasks": ["lib/main.dart", "lib/api_client.dart"],
    "vector-calendar": ["lib/main.dart", "lib/api_client.dart"],
    "vector-finance": ["lib/main.dart", "lib/api_client.dart"],
}

# A Dart read of the form  expr['key']  or  expr["key"].
READ_RE = re.compile(r"\[\s*'([a-z_][a-z0-9_]*)'\s*\]")

# Keys that are legitimately read off a Dart map that is NOT an API payload
# (local state, prefs, config) - or that the app deliberately treats as
# optional via a fallback. Each is justified, not just suppressed.
ALLOWED = {
    # local/shared_preferences and UI state maps
    "vector_api_base", "vector_api_key", "vector_user_id", "userId",
    "x-api-key", "x-user-id", "content-type",
    # fields the code reads with an explicit `?? fallback` for older payloads
    "id", "tasks", "goal", "startable", "done_today", "focus_minutes",
    "sessions", "date", "degraded", "note", "total", "done", "pct_done",
    "blocked_by_title", "startable_flag",
    # read off an ERROR body, which by definition is not in a success payload
    "error",
    # "task" is a shape PROBE, not a field read: upsertTask() checks
    # m.containsKey('task') to tolerate a backend that wraps the created row.
    # Verified live that POST /tasks returns the row flat, so no response ever
    # carries this key and the wrapper branch is simply never taken.
    "task",
}


def get(path: str):
    req = urllib.request.Request(BASE + path,
                                headers={"X-Api-Key": KEY, "X-User-Id": UID})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def read_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(READ_RE.findall(path.read_text(encoding="utf-8")))


def keys_of(payload) -> set[str]:
    """Every key present anywhere in a JSON payload, recursively."""
    found: set[str] = set()
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            found.update(cur.keys())
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


def main() -> int:
    fails: list[str] = []
    checked = 0

    # --- gather live payloads, one per endpoint the apps consume -------------
    payloads: dict[str, set[str]] = {}
    endpoints = ["/goals", "/tasks/startable", "/today", "/finance"]
    try:
        goals = get("/goals")
        payloads["/goals"] = keys_of(goals)
        if goals:
            gid = goals[0].get("goal_id")
            if gid:
                payloads["/goals/<id>/tasks"] = keys_of(get(f"/goals/{gid}/tasks"))
        payloads["/tasks/startable"] = keys_of(get("/tasks/startable"))
        payloads["/today"] = keys_of(get("/today"))
        payloads["/finance"] = keys_of(get("/finance"))
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: could not reach the API at {BASE}: {type(e).__name__}: {e}")
        print("      start it first, then re-run.")
        return 1

    # The union of everything the API exposes - a key present in ANY response
    # is a real API field. This catches renames (key gone everywhere) without
    # false-failing on a field that simply belongs to a different endpoint.
    api_keys: set[str] = set()
    for v in payloads.values():
        api_keys |= v

    print(f"API exposes {len(api_keys)} distinct keys across "
          f"{len(payloads)} endpoints\n")

    for app, rels in APPS.items():
        read: set[str] = set()
        for rel in rels:
            read |= read_keys(ROOT / app / rel)
        missing = sorted(k for k in read - api_keys - ALLOWED if k not in api_keys)
        # A key is only a problem if it is not an API field AND not allowed.
        missing = sorted(k for k in (read - api_keys - ALLOWED))
        checked += len(read)
        if missing:
            for k in missing:
                fails.append(f"{app}: reads '{k}' but no API response has it")
        else:
            print(f"  ok   {app}: all {len(read)} read keys are API fields")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        print(f"\n{len(fails)} field-contract violation(s)")
        return 1

    # --- regression pins ---------------------------------------------------
    # The union check above cannot catch this specific bug: `id` IS a real API
    # field (tasks use it), so a goal map reading `['id']` looks valid. Pin the
    # exact contract that broke: /goals keys the identifier as `goal_id`, and
    # the Dart goal-detail loader must read that key.
    pins = [
        ("/goals sends goal_id", "goal_id" in payloads["/goals"]),
        ("vector-tasks reads goal_id",
         "goal_id" in read_keys(ROOT / "vector-tasks/lib/main.dart")),
    ]
    # Every `widget.goal['id']` read must be a FALLBACK behind goal_id, not the
    # primary key. Checking the whole file for the literal would false-fail on
    # the correct `(goal_id ?? id)` form, so inspect line by line.
    bad_primary: list[str] = []
    for n, line in enumerate(
            (ROOT / "vector-tasks/lib/main.dart").read_text().splitlines(), 1):
        if "widget.goal['id']" in line and "goal_id" not in line:
            bad_primary.append(f"line {n}: {line.strip()}")
    pins.append((
        "widget.goal['id'] only used as a fallback behind goal_id",
        not bad_primary,
    ))
    pin_fail = [name for name, ok in pins if not ok]
    for name, ok in pins:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if pin_fail:
        print(f"\n{len(pin_fail)} regression pin(s) failed")
        return 1

    print("\nFIELD CONTRACT HELD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
