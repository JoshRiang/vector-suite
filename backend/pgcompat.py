"""A small sqlite3-compatible shim over psycopg2.

WHY this exists: store.py contains the query builder, the PostgREST filter
parser and the blocking logic, and it is covered by ~70 tests. Rewriting it for
Postgres would mean re-verifying all of that. Instead this module presents the
*subset* of the sqlite3 API that store.py actually uses, so the same tested code
runs on either engine.

The subset, deliberately narrow:
    connect(dsn) -> Connection
    Connection.execute(sql, params) -> Cursor
    Connection.executescript(sql)
    Connection.commit() / .rollback() / .close()
    Connection.row_factory (accepted and ignored - see Row)
    Cursor.fetchall() / .fetchone() / .close() / iteration
    Row["column"] and Row[0]

Two SQL differences are translated, and only two:
  1. `?` placeholders  -> `%s`      (outside string literals)
  2. `x is ?`          -> `x is not distinct from %s`
     Postgres has no `IS $1`; it needs IS [NOT] DISTINCT FROM for a
     parameterised null-safe comparison.
Literal `%` is escaped to `%%` so psycopg2 does not treat it as a format
placeholder.

Anything outside this subset raises rather than silently misbehaving.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Iterator, Sequence

import psycopg2
import psycopg2.extras


class Row:
    """Stands in for sqlite3.Row: supports row["col"] and row[0]."""

    __slots__ = ("_d",)

    def __init__(self, d: dict):
        self._d = d

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self._d.values())[key]
        return self._d[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._d.values())

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key: Any) -> bool:
        return key in self._d

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()

    def items(self):
        return self._d.items()

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def __repr__(self) -> str:
        return f"Row({self._d!r})"


def _to_pg(sql: str, has_params: bool = True) -> str:
    """`?` -> `%s`, escape literal `%`, null-safe `is ?` -> `is not distinct from`.

    `has_params` matters for the percent escaping only. psycopg2 interpolates
    `%` over the ENTIRE string whenever params are passed - it does not parse
    SQL - so a literal '100%' raises "unsupported format character" and must
    become '100%%'. With no params psycopg2 does not interpolate at all, and
    escaping would instead send a literal '%%' to the server.
    """
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote is not None:
            # Inside a literal, `%` still needs escaping when params are bound.
            if ch == "%" and has_params:
                out.append("%%")
                i += 1
                continue
            out.append(ch)
            if ch == quote:
                # A doubled quote is an escaped quote, not the end of the literal.
                if i + 1 < n and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "?":
            out.append("%s")
            i += 1
            continue
        if ch == "%":
            out.append("%%" if has_params else "%")
            i += 1
            continue
        out.append(ch)
        i += 1
    sql = "".join(out)
    # Only now, so the %s we just inserted is not re-escaped by the loop above.
    sql = re.sub(r"\bis\s+%s\b", "is not distinct from %s", sql, flags=re.IGNORECASE)
    return sql


def _split_statements(script: str) -> list[str]:
    """Split a DDL script on top-level semicolons.

    Handles three things that would otherwise split a statement in half:
      * single-quoted literals ('...', with '' as an escaped quote)
      * double-quoted identifiers ("...")
      * dollar-quoted bodies ($$ ... $$ or $tag$ ... $tag$), which Postgres
        uses for function/DO blocks and which legitimately contain semicolons
    """
    stmts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    dollar: str | None = None
    i, n = 0, len(script)
    while i < n:
        ch = script[i]

        if dollar is not None:
            if script.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            buf.append(ch)
            i += 1
            continue

        if quote is not None:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and script[i + 1] == quote:
                    buf.append(script[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue

        if ch == "$":
            m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", script[i:])
            if m:
                dollar = m.group(0)
                buf.append(dollar)
                i += len(dollar)
                continue
            buf.append(ch)
            i += 1
            continue

        if ch == ";":
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


class Cursor:
    def __init__(self, conn: "Connection"):
        self._conn = conn
        self._cur = conn._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self.lastrowid = None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> "Cursor":
        s = sql.strip()
        if s.upper().startswith("PRAGMA"):
            return self._pragma(s)
        try:
            self._cur.execute(_to_pg(s, bool(params)),
                              tuple(params) if params else None)
        except Exception:
            # Postgres aborts the ENTIRE transaction on a failed statement:
            # every later query on this connection then dies with
            # "current transaction is aborted, commands ignored until end of
            # transaction block". SQLite has no such behaviour - a bad statement
            # fails alone and the connection stays usable.
            #
            # store.py keeps one connection per thread and does not roll back
            # per statement, so without this a single rejected query (e.g. the
            # unknown-column guard, which is exercised by the tests and by any
            # bad client request) would permanently poison the API's connection
            # and every subsequent request would 500.
            #
            # Rolling back here restores SQLite's per-statement semantics.
            self._conn._raw.rollback()
            raise
        return self

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> "Cursor":
        rows = [tuple(p) for p in seq]
        self._cur.executemany(_to_pg(sql.strip(), bool(rows)), rows)
        return self

    # -- PRAGMA interception -------------------------------------------------
    def _pragma(self, s: str) -> "Cursor":
        m = re.match(r"PRAGMA\s+table_info\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", s, re.I)
        if m:
            table = m.group(1)
            self._cur.execute(
                """select column_name as name, data_type as type,
                          case when is_nullable = 'NO' then 1 else 0 end as "notnull"
                     from information_schema.columns
                    where table_schema = 'public' and table_name = %s
                    order by ordinal_position""",
                (table,),
            )
            return self
        # journal_mode / foreign_keys etc. are sqlite-only tuning: no-op.
        self._cur.execute("select 1 where false")
        return self

    # -- result access -------------------------------------------------------
    def fetchall(self) -> list[Row]:
        return [Row(d) for d in self._cur.fetchall()]

    def fetchone(self) -> Row | None:
        d = self._cur.fetchone()
        return Row(d) if d is not None else None

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())

    def close(self) -> None:
        self._cur.close()


class Connection:
    def __init__(self, dsn: str):
        self._raw = psycopg2.connect(dsn, connect_timeout=15)
        self.row_factory = None  # accepted for sqlite3 parity; Row is always used

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Cursor:
        return Cursor(self).execute(sql, params)

    def executescript(self, script: str) -> None:
        cur = self._raw.cursor()
        try:
            for stmt in _split_statements(script):
                cur.execute(stmt)
        finally:
            cur.close()

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def connect(dsn: str) -> Connection:
    return Connection(dsn)
