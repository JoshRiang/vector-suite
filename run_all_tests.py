#!/usr/bin/env python3
"""Run every VECTOR Suite check in one command.

Backend (pure logic, no network):   store, api routing, decomposition, timezone
Apps (structural, no toolchain):    check_apps.py
Field contract (needs live API):    test_field_contract.py - Dart field reads vs API keys
Postgres parity (only if a DSN is set): store behaviour + dialect differences

Exit code is non-zero if anything fails, so CI or a shell `&&` chain can gate
on it. This exists because the suite is now several files and running them by
memory is how a broken one gets skipped.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/josh/vector_suite")

SUITES = [
    ("store",         ROOT / "backend/test_store.py"),
    ("api routing",   ROOT / "backend/test_api.py"),
    ("goals + tasks", ROOT / "backend/test_goals.py"),
    ("decomposition", ROOT / "backend/test_decompose.py"),
    ("timezone",      ROOT / "backend/test_timezone.py"),
    ("auth",          ROOT / "backend/test_auth.py"),
    ("diag beacon",   ROOT / "backend/test_diag.py"),
    ("calendar days", ROOT / "backend/test_calendar.py"),
    ("command console", ROOT / "backend/test_commands.py"),
    ("daily brief",   ROOT / "backend/test_daily_brief.py"),
    ("flutter apps",  ROOT / "check_apps.py"),
    ("calendar grid", ROOT / "verify_calendar_grid.py"),
    ("late final", ROOT / "check_late_final.py"),
    ("endpoint fallback", ROOT / "verify_fallback.py"),
    ("beacon transport", ROOT / "verify_beacon_transport.py"),
    # Needs the live API; skips itself cleanly if unreachable. This is the only
    # suite that compares the field names the Dart reads against the keys the
    # API returns - the gap that let a broken tasks app pass every other check.
    ("field contract", ROOT / "backend/test_field_contract.py"),
]

# These need a live Postgres. They are skipped (not failed) without a DSN so the
# suite stays runnable offline, but they run whenever one is configured - which
# is exactly when a dialect regression would otherwise reach production.
PG_SUITES = [
    ("postgres parity",  ROOT / "backend/test_postgres.py"),
    ("postgres dialect", ROOT / "backend/test_pg_dialect.py"),
]


def _dsn() -> str:
    return (os.environ.get("DATABASE_URL")
            or os.environ.get("SUPABASE_DB_URL") or "").strip()


def main() -> int:
    failed: list[str] = []
    suites = list(SUITES)
    if _dsn():
        suites += PG_SUITES
    else:
        print("note: no DATABASE_URL set - skipping Postgres parity suites\n")

    for name, path in suites:
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
