#!/usr/bin/env python3
"""Probe the exact endpoints the widget's checkbox calls when tapped.

The widget posts to /tasks/{id}/done and /tasks/{id}/reopen on tap. If either is
missing or silently fails, the checkbox appears to do nothing - which is what
"make we can interact with task done from widget" would look like. So this
exercises the real path rather than trusting that the route exists.

Exit 0 = a tap can complete and reopen a task.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8790"
ENV = "/home/josh/vector_suite/backend/.env"


def env(name: str) -> str:
    for path in (ENV, "/home/josh/vector_suite/.env"):
        try:
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return ""


KEY = env("VECTOR_API_KEY")


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "X-API-Key": KEY, "X-User-Id": "josh",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def status_of(tid: str) -> str:
    """Read one task's status over HTTP.

    There is no GET /tasks (it answers {"error": "not_found"}), so the list has
    to come from /tasks/startable, which returns the open work. A task marked
    done therefore DISAPPEARS from it - that is how completion is observed here,
    and the test treats "gone" as done.
    """
    code, rows = call("GET", "/tasks/startable")
    if isinstance(rows, list):
        for r in rows:
            if str(r.get("id")) == tid:
                return str(r.get("status"))
        return "absent"
    return "?"


def main() -> int:
    # Create a throwaway task through the same POST the app uses.
    code, created = call("POST", "/tasks", {
        "title": "WIDGET TAP PROBE", "priority": 2, "status": "todo",
        "minutes": 10})
    # The API answers 201 for a create; accepting only 200 made this test fail
    # on a working endpoint.
    if code not in (200, 201) or not isinstance(created, dict) or not created.get("id"):
        print(f"FAIL create: http={code} {created}")
        return 1
    tid = created["id"]
    print(f"created {tid} status={status_of(tid)}")

    fails = 0

    # 1. Tap the checkbox: the widget calls /done. A completed task leaves
    #    /tasks/startable, so "absent" is the success signal.
    code, body = call("POST", f"/tasks/{tid}/done")
    st = status_of(tid)
    ok = code == 200 and st in ("absent", "done")
    print(f"  tap done   -> http={code} status={st} {'OK' if ok else 'FAIL'}")
    fails += 0 if ok else 1

    # 2. Tapping again must reopen, or the box is a one-way trap.
    code, body = call("POST", f"/tasks/{tid}/reopen")
    st = status_of(tid)
    ok = code == 200 and st == "todo"
    print(f"  tap reopen -> http={code} status={st} {'OK' if ok else 'FAIL'}")
    fails += 0 if ok else 1

    # 3. Skip, used by the widget's other action.
    code, body = call("POST", f"/tasks/{tid}/skip")
    print(f"  tap skip   -> http={code} {'OK' if code == 200 else 'FAIL'}")
    fails += 0 if code == 200 else 1

    # 4. A bad id must fail loudly, not report success.
    code, _ = call("POST", "/tasks/00000000-0000-0000-0000-000000000000/done")
    ok = code >= 400
    print(f"  bad id     -> http={code} {'OK (rejected)' if ok else 'FAIL (silent success)'}")
    fails += 0 if ok else 1

    # DELETE /tasks takes the id in the BODY, not as a query param. Passing it as
    # ?id=eq.<id> silently did nothing, so every probe run left a task behind in
    # the user's real list.
    code, _ = call("DELETE", "/tasks", {"id": tid})
    gone = status_of(tid) in ("absent", "?")
    print(f"  cleaned up: {gone} (http={code})")

    print()
    if fails:
        print(f"WIDGET TAP BROKEN: {fails} failure(s)")
        return 1
    print("WIDGET TAP VERIFIED: complete, reopen and skip all work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
