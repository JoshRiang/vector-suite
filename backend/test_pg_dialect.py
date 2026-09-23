"""Postgres-specific behaviours the SQLite build never exercised.

These are the dialect differences that would only show up in production, where
they are expensive: a poisoned connection makes EVERY later request fail, which
looks like "the whole backend is down" rather than "one query was rejected".

Run with a DSN in the environment:
    set -a; . ./.env; set +a
    python3 test_pg_dialect.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DSN = (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or "").strip()
if not DSN:
    print("SKIP: no DATABASE_URL/SUPABASE_DB_URL set")
    raise SystemExit(0)

PASS = FAIL = 0
TEST_SCHEMA = "vector_dialect_scratch"


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f"  [{extra}]" if extra else ""))


def _admin():
    import psycopg2
    return psycopg2.connect(DSN, connect_timeout=15)


def setup() -> None:
    import pgcompat
    con = _admin()
    con.autocommit = True
    cur = con.cursor()
    cur.execute(f"drop schema if exists {TEST_SCHEMA} cascade")
    cur.execute(f"create schema {TEST_SCHEMA}")
    cur.execute(f"set search_path to {TEST_SCHEMA}")
    for stmt in pgcompat._split_statements(
            Path(__file__).with_name("pg_schema.sql").read_text()):
        cur.execute(stmt)
    cur.close()
    con.close()


def teardown() -> None:
    con = _admin()
    con.autocommit = True
    cur = con.cursor()
    cur.execute(f"drop schema if exists {TEST_SCHEMA} cascade")
    cur.close()
    con.close()


def main() -> int:
    setup()
    sep = "&" if "?" in DSN else "?"
    os.environ["DATABASE_URL"] = f"{DSN}{sep}options=-c%20search_path%3D{TEST_SCHEMA}"
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "VECTOR_API_KEY"):
        os.environ.pop(k, None)

    import pgcompat

    print("\n1. a failed statement must not poison the connection")
    con = pgcompat.connect(os.environ["DATABASE_URL"])
    try:
        con.execute("insert into goals (id,user_id,title,status,created_at) "
                    "values (?,?,?,?,?)",
                    ["g1", "u1", "good", "active", "2026-09-23T10:00:00+07:00"])
        con.commit()
        check("valid insert works", True)
    except Exception as e:
        check("valid insert works", False, str(e)[:80])

    try:
        con.execute("select * from goals where nosuchcolumn = ?", ["x"])
        check("bad column raises", False, "expected an exception")
    except Exception:
        check("bad column raises", True)

    # THE regression: before the rollback-on-error fix this raised
    # InFailedSqlTransaction for every subsequent statement.
    try:
        rows = con.execute("select id from goals").fetchall()
        check("connection still usable after the failure", len(rows) == 1,
              f"got {len(rows)} rows")
    except Exception as e:
        check("connection still usable after the failure", False, str(e)[:90])

    try:
        con.execute("insert into goals (id,user_id,title,status,created_at) "
                    "values (?,?,?,?,?)",
                    ["g2", "u1", "second", "active", "2026-09-23T10:01:00+07:00"])
        con.commit()
        n = con.execute("select count(*) c from goals").fetchone()["c"]
        check("writes still work after the failure", n == 2, f"got {n}")
    except Exception as e:
        check("writes still work after the failure", False, str(e)[:90])
    con.close()

    print("\n2. null-safe comparison (`is ?` -> is not distinct from)")
    con = pgcompat.connect(os.environ["DATABASE_URL"])
    con.execute("insert into tasks (id,user_id,title,minutes,status,priority,"
                "source,created_at,blocked_by) values (?,?,?,?,?,?,?,?,?)",
                ["t1", "u1", "unblocked", 30, "todo", 3, "ai",
                 "2026-09-23T10:00:00+07:00", None])
    con.execute("insert into tasks (id,user_id,title,minutes,status,priority,"
                "source,created_at,blocked_by) values (?,?,?,?,?,?,?,?,?)",
                ["t2", "u1", "blocked", 30, "todo", 3, "ai",
                 "2026-09-23T10:00:00+07:00", "t1"])
    con.commit()
    is_null = con.execute("select id from tasks where blocked_by is ?",
                          [None]).fetchall()
    check("`is null` matches only the null row",
          [r["id"] for r in is_null] == ["t1"], str([r["id"] for r in is_null]))
    not_null = con.execute("select id from tasks where blocked_by is ?",
                           ["t1"]).fetchall()
    check("`is not-null value` matches", [r["id"] for r in not_null] == ["t2"])
    con.close()

    print("\n3. PRAGMA table_info shim (sqlite -> information_schema)")
    con = pgcompat.connect(os.environ["DATABASE_URL"])
    cols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
    check("column list returned", "blocked_by" in cols and "minutes" in cols,
          str(sorted(cols))[:80])
    check("unknown table yields nothing",
          con.execute("PRAGMA table_info(nope)").fetchall() == [])
    con.close()

    print("\n4. literal percent survives (psycopg2 treats % as a format char)")
    con = pgcompat.connect(os.environ["DATABASE_URL"])
    got = con.execute("select '100%' as pct where ? = ?", [1, 1]).fetchone()
    check("literal % not mangled", got is not None and got["pct"] == "100%",
          str(got))
    con.close()

    teardown()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
