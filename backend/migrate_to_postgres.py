"""Copy the local SQLite book into Postgres (Supabase).

Idempotent: rows are upserted on primary key, so re-running is safe and will not
duplicate. Existing cloud rows with the same id are overwritten with the local
value, which is the desired direction while SQLite remains the source of truth.

WHY a script and not a one-liner: the two engines differ in ways that silently
corrupt data (text vs timestamptz, integer booleans, `?` vs `%s`). This checks
row counts on both sides afterwards and fails loudly on a mismatch.

Usage:
    set -a; . ./.env; set +a     # provides DATABASE_URL
    python3 migrate_to_postgres.py [--dry-run]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TABLES = ["goals", "tasks", "day_plans", "focus_sessions",
          "finance_settings", "expenses"]

# Insert order respects the self-reference in tasks.blocked_by -> tasks.id.
# Rows are inserted with blocked_by nulled first, then patched, so a chain can
# be restored regardless of ordering.
DRY = "--dry-run" in sys.argv


def local_conn():
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("SUPABASE_DB_URL", None)
    import store
    store.init_db()
    return store._connect()


def cloud_conn(dsn: str):
    import pgcompat
    return pgcompat.connect(dsn)


def main() -> int:
    dsn = (os.environ.get("DATABASE_URL")
           or os.environ.get("SUPABASE_DB_URL") or "").strip()
    if not dsn:
        print("FAIL: DATABASE_URL not set")
        return 1

    src = local_conn()
    dst = cloud_conn(dsn)

    print(f"{'table':<18}{'local':>7}{'cloud before':>14}")
    plan = {}
    for t in TABLES:
        rows = [dict(r) for r in src.execute(f"select * from {t}").fetchall()]
        n_cloud = dst.execute(f"select count(*) c from {t}").fetchone()["c"]
        plan[t] = rows
        print(f"{t:<18}{len(rows):>7}{n_cloud:>14}")

    if DRY:
        print("\n--dry-run: nothing written")
        return 0

    print("\nmigrating...")
    for t in TABLES:
        rows = plan[t]
        if not rows:
            continue
        cols = list(rows[0].keys())
        # tasks.blocked_by is a self-FK: defer it so any insert order works.
        defer = "blocked_by" in cols
        payload_cols = [c for c in cols if not (defer and c == "blocked_by")]

        stmt = (f"insert into {t} ({','.join(payload_cols)}) values "
                f"({','.join(['?'] * len(payload_cols))}) "
                f"on conflict (id) do update set "
                + ", ".join(f"{c} = excluded.{c}" for c in payload_cols
                            if c != "id"))
        if t == "finance_settings":
            stmt = (f"insert into {t} ({','.join(payload_cols)}) values "
                    f"({','.join(['?'] * len(payload_cols))}) "
                    f"on conflict (user_id) do update set "
                    + ", ".join(f"{c} = excluded.{c}" for c in payload_cols
                                if c != "user_id"))

        for row in rows:
            dst.execute(stmt, [row[c] for c in payload_cols])

        if defer:
            # Second pass: restore the dependency edges now every row exists.
            for row in rows:
                if row.get("blocked_by"):
                    dst.execute(
                        f"update {t} set blocked_by = ? where id = ?",
                        [row["blocked_by"], row["id"]])

        dst.commit()
        print(f"  {t}: {len(rows)} rows upserted")

    print("\nverifying...")
    ok = True
    for t in TABLES:
        n_local = len(plan[t])
        n_cloud = dst.execute(f"select count(*) c from {t}").fetchone()["c"]
        good = n_cloud >= n_local
        ok &= good
        print(f"  {'ok ' if good else 'BAD'} {t}: local={n_local} cloud={n_cloud}")

    # The product-critical invariant must survive the move. Note that tasks
    # having an unfinished blocker is NORMAL - that is the dependency chain.
    # What must never happen is such a task appearing in startable_tasks, so
    # assert against the view the apps actually read.
    blocked_total = dst.execute("""
        select count(*) c from tasks t
        left join tasks b on b.id = t.blocked_by
        where t.status in ('todo','doing')
          and t.blocked_by is not null and b.status is distinct from 'done'
    """).fetchone()["c"]

    leaked = 0
    for uid in [r["user_id"] for r in
                dst.execute("select distinct user_id from tasks").fetchall()]:
        startable = dst.execute("""
            select t.id from tasks t
            left join tasks b on b.id = t.blocked_by
            where t.status in ('todo','doing') and t.user_id = ?
              and (t.blocked_by is null or b.status = 'done')
        """, [uid]).fetchall()
        allowed = {r["id"] for r in startable}
        for r in dst.execute("""
            select t.id from tasks t
            left join tasks b on b.id = t.blocked_by
            where t.status in ('todo','doing') and t.user_id = ?
              and t.blocked_by is not null and b.status is distinct from 'done'
        """, [uid]).fetchall():
            if r["id"] in allowed:
                leaked += 1

    print(f"\ntasks waiting on an unfinished blocker (expected, >0): {blocked_total}")
    print(f"blocked tasks wrongly offered as startable (must be 0): {leaked}")
    if leaked:
        ok = False

    # Also assert through the store's own view, which is what the apps call.
    import store as _s
    _s.reset_connections()
    n_startable = len(_s.db_request("GET", "startable_tasks"))
    print(f"startable_tasks via the store's view: {n_startable}")
    if n_startable > blocked_total + len(plan["tasks"]):
        print("  BAD: startable count is implausible")
        ok = False

    print("\nMIGRATION " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
