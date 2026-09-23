"""Run the store's own behavioural tests against a Postgres backend.

WHY: store.py's query builder, PostgREST filter parser and blocking logic now
run on either SQLite or Postgres. "It works on SQLite" is not evidence that it
works on Postgres - the dialects differ (`is ?`, `%` escaping, PRAGMA vs
information_schema, RETURNING). This script points the same test_store.py
assertions at a live Postgres database, so a dialect bug fails a test instead of
corrupting the user's real data.

It creates a dedicated throwaway SCHEMA and drops it afterwards, so it is safe
to run against the production database: real rows are never touched.

Usage:
    set -a; . ./.env; set +a
    python3 test_postgres.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DSN = (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or "").strip()
if not DSN:
    print("SKIP: no DATABASE_URL/SUPABASE_DB_URL set")
    raise SystemExit(0)

TEST_SCHEMA = "vector_test_scratch"


def _admin():
    import psycopg2
    return psycopg2.connect(DSN, connect_timeout=15)


def setup_scratch_schema() -> None:
    import pgcompat
    con = _admin()
    con.autocommit = True
    cur = con.cursor()
    cur.execute(f"drop schema if exists {TEST_SCHEMA} cascade")
    cur.execute(f"create schema {TEST_SCHEMA}")
    cur.execute(f"set search_path to {TEST_SCHEMA}")
    ddl = Path(__file__).with_name("pg_schema.sql").read_text()
    for stmt in pgcompat._split_statements(ddl):
        cur.execute(stmt)
    cur.close()
    con.close()


def drop_scratch_schema() -> None:
    con = _admin()
    con.autocommit = True
    cur = con.cursor()
    cur.execute(f"drop schema if exists {TEST_SCHEMA} cascade")
    cur.close()
    con.close()


def count_rows_in_public() -> int:
    """Rows in the real schema - must stay 0 for this script to be safe."""
    con = _admin()
    cur = con.cursor()
    total = 0
    for t in ("goals", "tasks", "day_plans", "focus_sessions", "expenses"):
        try:
            cur.execute(f"select count(*) from public.{t}")
            total += cur.fetchone()[0]
        except Exception:
            con.rollback()
    cur.close()
    con.close()
    return total


def main() -> int:
    before = count_rows_in_public()

    # Every connection the store opens must land in the scratch schema, so the
    # setting rides along in the DSN rather than being issued once.
    sep = "&" if "?" in DSN else "?"
    os.environ["DATABASE_URL"] = (
        f"{DSN}{sep}options=-c%20search_path%3D{TEST_SCHEMA}")
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "VECTOR_API_KEY"):
        os.environ.pop(k, None)
    os.environ["VECTOR_DB"] = "/nonexistent/ignore-me.db"

    setup_scratch_schema()
    try:
        import test_store
        rc = test_store.main()
    finally:
        drop_scratch_schema()

    after = count_rows_in_public()
    print()
    print(f"rows in the real (public) schema: {before} -> {after}")
    if after != before:
        print("FAIL: this script modified real data")
        return 1
    print("scratch schema dropped; production data untouched")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
