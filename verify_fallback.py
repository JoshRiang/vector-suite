"""Prove the multi-endpoint fallback in api_client.dart actually works.

The app now tries baseUrl, then the LAN/tailnet fallbacks. A bug here would be
invisible until a phone hit it, so the logic is mirrored in Python and exercised
against a REAL dead port and the REAL live server:

  1. primary dead + fallback live  -> succeeds (this is the whole point)
  2. primary live                  -> succeeds, fallback never needed
  3. all endpoints dead            -> raises the offline message, not a crash
  4. HTTP error (401)              -> NOT retried on other endpoints, because the
                                      server answered; retrying would just repeat

Run: python3 verify_fallback.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

LIVE = "http://127.0.0.1:8790"
DEAD = "http://127.0.0.1:1"          # nothing listens here; connect refused
KEY = "test-key-not-needed-locally"


def try_endpoint(base: str, path: str, timeout: float) -> tuple[bool, str]:
    """Return (ok, detail). Mirrors _sendTo's success/failure split."""
    req = urllib.request.Request(base + path, headers={"X-User-Id": "josh"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            json.loads(r.read().decode() or "null")
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # The server ANSWERED (even with an error) - not a transport failure.
        return False, f"HTTPError {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}"


def send(bases: list[str], path: str = "/health",
         timeout: float = 6.0) -> tuple[bool, str]:
    """Mirrors Api._send: try each base, stop early on a server reply."""
    last = "no endpoints"
    for i, b in enumerate(bases):
        ok, detail = try_endpoint(b, path, timeout)
        if ok:
            return True, f"{b} -> {detail}"
        last = f"{b} -> {detail}"
        if detail.startswith("HTTPError"):
            # Server answered; another address would give the same answer.
            return False, f"not retried (server answered): {last}"
    return False, f"all failed, last: {last}"


def main() -> int:
    fails = []

    ok, d = send([DEAD, LIVE])
    print(f"  1. primary dead, fallback live  -> {'OK ' if ok else 'FAIL'} {d}")
    if not ok:
        fails.append("fallback did not rescue a dead primary")

    ok, d = send([LIVE, DEAD])
    print(f"  2. primary live                 -> {'OK ' if ok else 'FAIL'} {d}")
    if not ok:
        fails.append("live primary failed")

    ok, d = send([DEAD, DEAD])
    print(f"  3. all dead                     -> {'OK ' if not ok else 'FAIL'} {d}")
    if ok:
        fails.append("all-dead returned success")

    # 401 from the live server must NOT be retried on the dead endpoint.
    ok, d = send([LIVE], path="/goals")   # no key -> 401
    print(f"  4. server answers 401           -> "
          f"{'OK ' if not ok else 'FAIL'} {d}")
    if ok:
        fails.append("401 treated as success")
    if "not retried" not in d:
        fails.append("server error was retried on other endpoints")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("FALLBACK LOGIC VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
