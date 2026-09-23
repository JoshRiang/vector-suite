"""Tests for the API's shared-secret auth.

WHY THIS MATTERS: the API trusts the X-User-Id header. On a Tailscale-private
network that is fine, but the moment the service is reachable from the public
internet, an unauthenticated API means anyone who learns the URL can read the
owner's finances and write tasks. These tests pin the boundary.

Runs with no network and no LLM.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile

# Hermetic: these tests must run on a throwaway SQLite file. If a
# DATABASE_URL is present (e.g. the Supabase DSN in backend/.env) the
# store would connect to the LIVE cloud database and the tests would
# write there. Clear it so the suite always targets a temp file.
for _k in ("DATABASE_URL", "SUPABASE_DB_URL"):
    os.environ.pop(_k, None)
os.environ["VECTOR_DB"] = os.path.join(tempfile.mkdtemp(), "auth.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api  # noqa: E402
import store  # noqa: E402

PASS = FAIL = 0
UID = "auth-user"


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def reload_with_key(key: str):
    """Re-import api with VECTOR_API_KEY set to `key` ('' disables auth)."""
    os.environ["VECTOR_API_KEY"] = key
    importlib.reload(api)
    return api


def main() -> int:
    store.init_db()

    print("auth disabled (VECTOR_API_KEY unset)")
    m = reload_with_key("")
    st, _, body = m.route("GET", "/tasks/startable", {}, UID, None)
    check("data is reachable when no key is configured", st == 200,
          f"status={st} body={body[:120]}")
    st, _, body = m.route("GET", "/health", {}, None, None)
    check("health reports auth disabled", b'"auth": false' in body, body[:160])

    print("\nauth enabled")
    m = reload_with_key("s3cret-key")
    st, _, body = m.route("GET", "/tasks/startable", {}, UID, None)
    check("no key -> 401", st == 401, f"status={st}")
    check("401 body says unauthorized", b"unauthorized" in body, body[:160])

    st, _, body = m.route("GET", "/tasks/startable", {}, UID, "wrong")
    check("wrong key -> 401", st == 401, f"status={st}")

    st, _, body = m.route("GET", "/tasks/startable", {}, UID, "s3cret-key")
    check("correct key -> 200", st == 200, f"status={st} body={body[:120]}")

    print("\nhealth stays open (no user data, needed for uptime probes)")
    st, _, body = m.route("GET", "/health", {}, None, None)
    check("health reachable without a key", st == 200, f"status={st}")
    check("health reports auth enabled", b'"auth": true' in body, body[:160])
    check("health leaks no user data",
          b"balance" not in body and b"task" not in body, body[:200])

    print("\nauth is required on EVERY data route, not just one")
    routes = [
        ("GET", "/tasks/startable", None),
        ("GET", "/today", None),
        ("GET", "/productivity", None),
        ("GET", "/finance", None),
        ("GET", "/goals", None),
        ("POST", "/goals", {"title": "x"}),
        ("POST", "/expenses", {"amount": 1}),
        ("PATCH", "/tasks", {"id": "x", "status": "done"}),
    ]
    for method, path, body in routes:
        st, _, _ = m.route(method, path, body or {}, UID, None)
        check(f"{method} {path} blocked without key", st == 401, f"status={st}")

    print("\nwrites cannot slip through unauthenticated")
    before = len(m.db_request("GET", "tasks", params={"user_id": f"eq.{UID}"}))
    m.route("POST", "/goals", {"title": "sneaky"}, UID, None)
    after = len(m.db_request("GET", "tasks", params={"user_id": f"eq.{UID}"}))
    check("unauthenticated POST created no tasks", before == after,
          f"{before} -> {after}")

    print("\nuser_id is still required even with a valid key")
    st, _, body = m.route("GET", "/tasks/startable", {}, None, "s3cret-key")
    check("missing user id -> 401", st == 401, f"status={st}")

    print("\nconstant-time compare is used")
    import inspect
    src = inspect.getsource(m._auth_ok)
    check("uses hmac.compare_digest", "compare_digest" in src)

    reload_with_key("")
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
