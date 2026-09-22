#!/usr/bin/env python3
"""
Run every VECTOR Suite check in one command.

Backend (pure logic, no network):   store, api routing, decomposition, timezone
Apps (structural, no toolchain):    check_apps.py

Exit code is non-zero if anything fails, so CI or a shell `&&` chain can gate
on it. This exists because the suite is now four files and running them by
memory is how a broken one gets skipped.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/josh/vector_suite")

SUITES = [
    ("store",         ROOT / "backend/test_store.py"),
    ("api routing",   ROOT / "backend/test_api.py"),
    ("decomposition", ROOT / "backend/test_decompose.py"),
    ("timezone",      ROOT / "backend/test_timezone.py"),
    ("daily brief",   ROOT / "backend/test_daily_brief.py"),
    ("flutter apps",  ROOT / "check_apps.py"),
]


def main() -> int:
    failed: list[str] = []
    for name, path in SUITES:
        print("=" * 62)
        print(f"  {name}  ({path.name})")
        print("=" * 62)
        if not path.exists():
            print(f"  MISSING: {path}")
            failed.append(name)
            continue
        r = subprocess.run([sys.executable, str(path)],
                           capture_output=True, text=True, cwd=str(path.parent))
        out = (r.stdout or "") + (r.stderr or "")
        # Show only the tail: the per-assertion lines are long.
        lines = [l for l in out.strip().splitlines() if l.strip()]
        for line in lines[-6:]:
            print("  " + line)
        if r.returncode != 0:
            failed.append(name)
        print()

    print("=" * 62)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("ALL SUITES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
