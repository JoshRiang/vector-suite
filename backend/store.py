"""
Local persistence for the VECTOR Suite backend.

WHY THIS EXISTS
---------------
The schema targets Supabase (private Postgres). But requiring a cloud signup
before the product works at all is the wrong order: the core question is
whether the goal->plan loop actually helps, and that has to be answerable
today, on this box.

So the backend talks to a `db_request()` interface with PostgREST semantics,
and this module implements that interface over local SQLite. Switching to
Supabase later is one environment variable -- no handler changes, no schema
rewrite.

SEMANTICS IMPLEMENTED (the subset api.py actually uses)
-------------------------------------------------------
  GET    /table?col=eq.VALUE&order=a.asc,b.desc&limit=N&select=a,b
  POST   /table            body=row | [rows]      -> inserted rows
  PATCH  /table?col=eq.V -> updated rows
  DELETE /table?col=eq.V -> deleted rows

Plus the three views from schema.sql: startable_tasks, goal_progress,
daily_productivity. The startable_tasks filter is the product's core
behaviour -- it must never return a blocked task.

Rows are returned with TEXT timestamps and INTEGER ids, matching what the
Flutter client parses.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

DB_PATH = os.environ.get("VECTOR_DB", "/home/josh/vector_suite/data/vector.db")

# Postgres (Supabase) takes precedence when a DSN is configured. Unset, the
# store falls back to local SQLite so the apps work with no cloud dependency.
# Both paths run this same query builder, so tests cover either engine.
PG_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "pg_schema.sql")

_local = threading.local()


def _dsn() -> str:
    """Connection string, if a Postgres backend is configured."""
    return (os.environ.get("DATABASE_URL", "")
            or os.environ.get("SUPABASE_DB_URL", "")).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------
def _connect():
    """One connection per thread; neither sqlite nor psycopg objects are
    safe to share across threads."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        dsn = _dsn()
        if dsn:
            import pgcompat
            conn = pgcompat.connect(dsn)
        else:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            conn = sqlite3.connect(DB_PATH, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


SCHEMA = """
create table if not exists goals (
  id           text primary key,
  user_id      text not null,
  title        text not null,
  detail       text,
  target_date  text,
  status       text not null default 'active',
  created_at   text not null,
  completed_at text
);
create index if not exists goals_user_status_idx on goals(user_id, status);

create table if not exists tasks (
  id           text primary key,
  user_id      text not null,
  goal_id      text,
  title        text not null,
  why          text,
  minutes      integer not null default 30,
  blocked_by   text,
  status       text not null default 'todo',
  priority     integer not null default 3,
  source       text not null default 'ai',
  scheduled_at text,
  created_at   text not null,
  completed_at text
);
create index if not exists tasks_user_status_idx on tasks(user_id, status);
create index if not exists tasks_goal_idx        on tasks(goal_id);

create table if not exists day_plans (
  id              text primary key,
  user_id         text not null,
  plan_date       text not null,
  planned_minutes integer not null default 0,
  actual_minutes  integer not null default 0,
  summary         text,
  created_at      text not null,
  unique(user_id, plan_date)
);

create table if not exists focus_sessions (
  id         text primary key,
  user_id    text not null,
  task_id    text,
  started_at text not null,
  ended_at   text,
  minutes    integer,
  completed  integer not null default 0
);
create index if not exists focus_user_started_idx on focus_sessions(user_id, started_at);

create table if not exists finance_settings (
  user_id        text primary key,
  currency       text not null default 'IDR',
  balance        real not null default 0,
  daily_budget   real not null default 0,
  target_date    text,
  monthly_income real not null default 0,
  updated_at     text not null
);

create table if not exists expenses (
  id         text primary key,
  user_id    text not null,
  amount     real not null,
  note       text,
  category   text,
  spent_on   text not null,
  created_at text not null
);
create index if not exists expenses_user_date_idx on expenses(user_id, spent_on);
"""

TABLES = {
    "goals", "tasks", "day_plans", "focus_sessions",
    "finance_settings", "expenses",
}
VIEWS = {"startable_tasks", "goal_progress", "daily_productivity"}

# Columns the API is allowed to write, per table. Prevents an unknown JSON key
# from reaching the SQL builder as an identifier.
WRITABLE = {
    "goals": {"user_id", "title", "detail", "target_date", "status",
              "completed_at"},
    "tasks": {"user_id", "goal_id", "title", "why", "minutes", "blocked_by",
              "status", "priority", "source", "scheduled_at", "completed_at"},
    "day_plans": {"user_id", "plan_date", "planned_minutes", "actual_minutes",
                  "summary"},
    "focus_sessions": {"user_id", "task_id", "started_at", "ended_at",
                       "minutes", "completed"},
    "finance_settings": {"user_id", "currency", "balance", "daily_budget",
                         "target_date", "monthly_income", "updated_at"},
    "expenses": {"user_id", "amount", "note", "category", "spent_on"},
}

DEFAULTS = {
    "goals": {"status": "active"},
    "tasks": {"minutes": 30, "status": "todo", "priority": 3, "source": "ai"},
    "day_plans": {"planned_minutes": 0, "actual_minutes": 0},
    "focus_sessions": {"completed": 0},
    "finance_settings": {"currency": "IDR", "balance": 0, "daily_budget": 0,
                         "monthly_income": 0},
    "expenses": {},
}

_initialised = False


def init_db() -> None:
    global _initialised
    if _initialised:
        return
    conn = _connect()
    if _dsn():
        # Postgres: run the mirrored schema. Kept as a separate file so the
        # exact DDL applied to the cloud database is reviewable in one place.
        with open(PG_SCHEMA_PATH, encoding="utf-8") as fh:
            conn.executescript(fh.read())
    else:
        conn.executescript(SCHEMA)
    conn.commit()
    _initialised = True


def reset_connections() -> None:
    """Drop the thread-local connection (used by tests after switching DB)."""
    global _initialised
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
    _initialised = False


# ---------------------------------------------------------------------------
# Query-string parsing (PostgREST subset)
# ---------------------------------------------------------------------------
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str, allowed: set[str]) -> str:
    if name not in allowed:
        raise RuntimeError(f"unknown_column:{name}")
    if not _IDENT.match(name):
        raise RuntimeError(f"bad_column:{name}")
    return name


def _parse_filters(params: dict[str, str]) -> tuple[list[str], list[Any]]:
    """Turn PostgREST-style params into SQL WHERE fragments + bind values."""
    clauses: list[str] = []
    values: list[Any] = []
    for key, raw in params.items():
        if key in ("order", "limit", "select", "offset"):
            continue
        if not _IDENT.match(key):
            raise RuntimeError(f"bad_filter:{key}")
        op, _, val = str(raw).partition(".")
        if not _:
            op, val = "eq", str(raw)

        if op == "eq":
            clauses.append(f"{key} = ?")
            values.append(val)
        elif op == "neq":
            clauses.append(f"{key} <> ?")
            values.append(val)
        elif op == "gt":
            clauses.append(f"{key} > ?")
            values.append(val)
        elif op == "gte":
            clauses.append(f"{key} >= ?")
            values.append(val)
        elif op == "lt":
            clauses.append(f"{key} < ?")
            values.append(val)
        elif op == "lte":
            clauses.append(f"{key} <= ?")
            values.append(val)
        elif op == "in":
            items = [v for v in val.strip("()").split(",") if v]
            if not items:
                clauses.append("0 = 1")
            else:
                clauses.append(f"{key} in ({','.join('?' * len(items))})")
                values.extend(items)
        elif op == "is":
            if val.lower() == "null":
                clauses.append(f"{key} is null")
            else:
                clauses.append(f"{key} is ?")
                values.append(val)
        else:
            raise RuntimeError(f"unsupported_operator:{op}")
    return clauses, values


def _order_clause(order: str, allowed: set[str]) -> str:
    parts = []
    for token in order.split(","):
        token = token.strip()
        if not token:
            continue
        col, _, direction = token.partition(".")
        _safe_ident(col, allowed)
        parts.append(f"{col} {'DESC' if direction.lower() == 'desc' else 'ASC'}")
    return (" order by " + ", ".join(parts)) if parts else ""


def _project(rows: list[dict], select: str | None) -> list[dict]:
    if not select or select.strip() == "*":
        return rows
    cols = [c.strip() for c in select.split(",") if c.strip()]
    out = []
    for r in rows:
        out.append({c: r.get(c) for c in cols})
    return out


# ---------------------------------------------------------------------------
# Views (computed, never stored)
# ---------------------------------------------------------------------------
def _view_rows(name: str, user_id: str | None) -> list[dict]:
    conn = _connect()
    if name == "startable_tasks":
        # THE product-critical query: a task is startable only if it is not
        # finished AND its blocker (if any) is done. Returning a blocked task
        # here would recreate the paralysis the product exists to remove.
        sql = """
        select t.* from tasks t
        left join tasks b on b.id = t.blocked_by
        where t.status in ('todo','doing')
          and (t.blocked_by is null or b.status = 'done')
        """
        args: list[Any] = []
        if user_id:
            sql += " and t.user_id = ?"
            args.append(user_id)
        sql += " order by t.priority asc, t.minutes asc"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    if name == "goal_progress":
        sql = """
        select g.id as goal_id, g.user_id, g.title, g.status,
               count(t.id) as total_tasks,
               coalesce(sum(case when t.status='done' then 1 else 0 end),0) as done_tasks,
               coalesce(sum(case when t.status='done' then t.minutes else 0 end),0) as minutes_done,
               coalesce(sum(t.minutes),0) as minutes_total,
               case when count(t.id)=0 then 0
                    else cast(round(100.0 * coalesce(sum(case when t.status='done' then 1 else 0 end),0)
                         / count(t.id)) as integer) end as pct_done
        from goals g left join tasks t on t.goal_id = g.id
        """
        args = []
        if user_id:
            sql += " where g.user_id = ?"
            args.append(user_id)
        sql += " group by g.id, g.user_id, g.title, g.status"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    if name == "daily_productivity":
        sql = """
        select user_id, substr(started_at,1,10) as day,
               count(*) as sessions,
               coalesce(sum(minutes),0) as minutes,
               coalesce(sum(case when completed = 1 then 1 else 0 end),0) as completed
        from focus_sessions
        """
        args = []
        if user_id:
            sql += " where user_id = ?"
            args.append(user_id)
        sql += " group by user_id, substr(started_at,1,10)"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    raise RuntimeError(f"unknown_view:{name}")


# ---------------------------------------------------------------------------
# Public interface -- mirrors api.db_request
# ---------------------------------------------------------------------------
def db_request(method: str, path: str, *, params: dict | None = None,
               body: Any = None, timeout: int = 30) -> Any:
    init_db()
    conn = _connect()
    params = dict(params or {})
    table = path.strip("/").split("/")[0]

    if table in VIEWS:
        user_filter = None
        raw = params.get("user_id", "")
        if raw.startswith("eq."):
            user_filter = raw[3:]
        return _view_rows(table, user_filter)

    if table not in TABLES:
        raise RuntimeError(f"unknown_table:{table}")

    allowed = WRITABLE[table]
    select = params.get("select")

    if method == "GET":
        where, values = _parse_filters(params)
        sql = f"select * from {table}"
        if where:
            sql += " where " + " and ".join(where)
        sql += _order_clause(params.get("order", ""), allowed)
        if params.get("limit"):
            try:
                sql += f" limit {int(params['limit'])}"
            except (TypeError, ValueError):
                raise RuntimeError("bad_limit") from None
        rows = [dict(r) for r in conn.execute(sql, values).fetchall()]
        return _project(rows, select)

    if method == "POST":
        items = body if isinstance(body, list) else [body]
        out = []
        for item in items:
            row = dict(item or {})
            row.pop("id", None)
            cols_present = _columns(table)
            # Only synthesise `id` for tables that actually have one --
            # finance_settings is keyed by user_id alone.
            if "id" in cols_present:
                row.setdefault("id", str(uuid.uuid4()))
            for k, v in DEFAULTS.get(table, {}).items():
                row.setdefault(k, v)
            unknown = set(row) - allowed - {"id"}
            if unknown:
                raise RuntimeError(f"unknown_column:{sorted(unknown)[0]}")
            if "created_at" in cols_present:
                row.setdefault("created_at", _now())
            if table == "finance_settings":
                row.setdefault("updated_at", _now())
            if table == "expenses":
                row.setdefault("spent_on", date.today().isoformat())
            cols = list(row)
            sql = (f"insert into {table} ({','.join(cols)}) "
                   f"values ({','.join('?' * len(cols))})")
            conn.execute(sql, [row[c] for c in cols])
            out.append(row)
        conn.commit()
        # Re-read so generated/defaulted columns are returned as stored.
        if "id" in _columns(table):
            ids = [r["id"] for r in out]
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            rows = [dict(r) for r in conn.execute(
                f"select * from {table} where id in ({placeholders})", ids).fetchall()]
            order = {i: n for n, i in enumerate(ids)}
            rows.sort(key=lambda r: order.get(r["id"], 0))
        else:
            keys = [r["user_id"] for r in out]
            if not keys:
                return []
            placeholders = ",".join("?" * len(keys))
            rows = [dict(r) for r in conn.execute(
                f"select * from {table} where user_id in ({placeholders})",
                keys).fetchall()]
        return _project(rows, select)

    if method == "PATCH":
        patch = dict(body or {})
        unknown = set(patch) - allowed
        if unknown:
            raise RuntimeError(f"unknown_column:{sorted(unknown)[0]}")
        if not patch:
            raise RuntimeError("nothing_to_update")
        where, values = _parse_filters(params)
        sql = f"update {table} set " + ", ".join(f"{k} = ?" for k in patch)
        args = list(patch.values()) + values
        if where:
            sql += " where " + " and ".join(where)
        conn.execute(sql, args)
        conn.commit()
        sel_where, sel_vals = _parse_filters(params)
        sel = f"select * from {table}"
        if sel_where:
            sel += " where " + " and ".join(sel_where)
        return [dict(r) for r in conn.execute(sel, sel_vals).fetchall()]

    if method == "DELETE":
        where, values = _parse_filters(params)
        sql = f"delete from {table}"
        if where:
            sql += " where " + " and ".join(where)
        conn.execute(sql, values)
        conn.commit()
        return []

    raise RuntimeError(f"unsupported_method:{method}")


def _columns(table: str) -> set[str]:
    conn = _connect()
    return {r["name"] for r in
            conn.execute(f"PRAGMA table_info({table})").fetchall()}
