#!/usr/bin/env python3
"""Prove the API key inside a shipped APK is the SAME VALUE the server expects.

The resource verifier can only report a secret's LENGTH, so a build with the
right length and the wrong characters looks identical to a correct one -- and
would produce exactly the symptom this was written for: 401 on the phone while
every local check is green.

This hashes both sides and compares digests. It never prints the key.

Usage:
    python3 scripts/verify_key_match.py <apk> [<apk> ...]
"""
import hashlib
import logging
import os
import sys

sys.path.insert(0, os.path.expanduser(
    "~/.hermes/skills/software-development/flutter-android-build/scripts"))
from verify_apk_resources import load, resolve  # noqa: E402

SERVER_ENV = "/home/josh/vector_suite/backend/.env"


def server_key() -> str:
    key = ""
    with open(SERVER_ENV) as fh:
        for line in fh:
            if line.startswith("VECTOR_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("no VECTOR_API_KEY in " + SERVER_ENV)
    return key


def main() -> int:
    logging.disable(logging.CRITICAL)
    apks = sys.argv[1:]
    if not apks:
        sys.exit("usage: verify_key_match.py <apk> [<apk> ...]")

    want = server_key()
    want_digest = hashlib.sha256(want.encode()).hexdigest()
    print(f"server key : {len(want)} chars  sha256={want_digest[:16]}")

    bad = 0
    for path in apks:
        _a, pkg, res = load(path)
        got = resolve(res, pkg, "vector_api_key") or ""
        got_digest = hashlib.sha256(got.encode()).hexdigest()
        name = os.path.basename(path)
        if not got:
            print(f"  FAIL {name}: no vector_api_key value in the APK")
            bad += 1
        elif got_digest == want_digest:
            print(f"  ok   {name}: key matches the server exactly ({len(got)} chars)")
        else:
            print(f"  FAIL {name}: key DIFFERS from the server "
                  f"({len(got)} chars, sha256={got_digest[:16]})")
            print(f"       server starts {want[:4]!r}, apk starts {got[:4]!r}")
            bad += 1

    print("\nALL KEYS MATCH" if not bad else f"\n{bad} MISMATCH(ES)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
