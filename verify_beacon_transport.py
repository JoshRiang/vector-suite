"""End-to-end check of the beacon over real HTTP, not just route().

The first beacon attempt logged "stage=?" - the stage was lost in transport -
so this exercises the actual socket path with a query string and with a body,
and confirms both land in diag.log. Testing route() alone would have missed it.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

LOG = Path("/home/josh/vector_suite/data/diag.log")
BASE = "http://127.0.0.1:8790"


def beacon_http(stage: str, detail: str) -> int:
    """POST /diag exactly the way the app does: query string AND JSON body."""
    import json
    from urllib.parse import urlencode
    url = f"{BASE}/diag?{urlencode({'stage': stage, 'detail': detail})}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"stage": stage, "detail": detail}).encode(),
        headers={"Content-Type": "application/json", "X-User-Id": "josh"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def beacon_query_only(stage: str) -> int:
    """POST with NO body - what a client with a broken body transport sends."""
    from urllib.parse import urlencode
    url = f"{BASE}/diag?{urlencode({'stage': stage, 'detail': 'query-only'})}"
    req = urllib.request.Request(
        url, data=b"", headers={"X-User-Id": "josh"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def main() -> int:
    fails = []

    # 1. query string + body
    before = LOG.read_text().count("\n") if LOG.exists() else 0
    if beacon_http("e2e-both", "both transports") != 200:
        fails.append("query+body beacon did not return 200")
    line = LOG.read_text().rstrip().splitlines()[-1]
    if "e2e-both" not in line:
        fails.append(f"stage lost with query+body: {line!r}")
    print(f"  1. query string + body  -> {line.strip()}")

    # 2. query string ONLY (empty body) - the case that broke
    if beacon_query_only("e2e-queryonly") != 200:
        fails.append("query-only beacon did not return 200")
    line = LOG.read_text().rstrip().splitlines()[-1]
    if "e2e-queryonly" not in line:
        fails.append(f"stage lost with an empty body: {line!r}")
    print(f"  2. query string only    -> {line.strip()}")

    # 3. the stage must never be recorded as '?'
    if "stage=?" in LOG.read_text():
        fails.append("a beacon was still logged with stage=?")
    else:
        print("  3. no stage=? in the log")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("BEACON TRANSPORT VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
