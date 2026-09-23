"""The /diag beacon must work WITHOUT auth - that is the whole point.

The apps render white on a device and report nothing, so a beacon is the only
way to learn where startup stops. If /diag were behind the API-key gate it would
be useless in exactly the situation it exists for (a device that cannot complete
a normal request), so this pins that it stays open and that it actually records
what was sent.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api  # noqa: E402

LOG = Path("/home/josh/vector_suite/data/diag.log")


def test_diag_needs_no_auth() -> list[str]:
    fails = []
    status, _, payload = api.route(
        "POST", "/diag", {"stage": "test-stage", "detail": "test-detail"},
        "josh", api_key=None,
    )
    if status != 200:
        fails.append(f"/diag without a key returned {status}, expected 200")
    if b"ok" not in payload:
        fails.append("/diag did not return ok")
    return fails


def test_diag_records_the_beacon() -> list[str]:
    fails = []
    before = LOG.read_text().count("\n") if LOG.exists() else 0
    api.route("POST", "/diag",
              {"stage": "unit-test", "detail": "beacon body"},
              "josh", api_key=None)
    after = LOG.read_text().count("\n") if LOG.exists() else 0
    if after <= before:
        fails.append("no line appended to diag.log")
    else:
        last = LOG.read_text().rstrip().splitlines()[-1]
        if "unit-test" not in last or "beacon body" not in last:
            fails.append(f"logged line missing content: {last!r}")
    return fails


def test_diag_is_not_a_data_route() -> list[str]:
    """It must not become a way to read or write real user data."""
    fails = []
    # GET must not be treated as the beacon.
    status, _, _ = api.route("GET", "/diag", {}, "josh", api_key=None)
    if status == 200:
        fails.append("GET /diag returned 200; the beacon is POST-only")
    return fails


def test_other_routes_still_require_auth() -> list[str]:
    """Widening /diag must not have opened anything else."""
    fails = []
    if not api.API_KEY:
        return ["API_KEY not configured - cannot test the gate"]
    for path in ["/goals", "/today", "/finance"]:
        status, _, _ = api.route("GET", path, {}, "josh", api_key=None)
        if status != 401:
            fails.append(f"{path} without a key returned {status}, expected 401")
    return fails


def main() -> int:
    total = 0
    failed = 0
    for fn in (test_diag_needs_no_auth, test_diag_records_the_beacon,
               test_diag_is_not_a_data_route, test_other_routes_still_require_auth):
        total += 1
        fails = fn()
        if fails:
            failed += 1
            print(f"FAIL {fn.__name__}")
            for f in fails:
                print(f"       {f}")
        else:
            print(f"  ok {fn.__name__}")
    print(f"\n{total - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
