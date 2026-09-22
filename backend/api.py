"""
VECTOR Suite API — one backend for three apps (tasks / calendar / finance).

WHY A BACKEND AT ALL
--------------------
The apps could talk to Supabase directly, and they do for plain CRUD. But the
LLM key must never ship inside an APK (it is trivially extractable), and goal
decomposition is the product's core value. So anything that calls a model, or
that needs to reason across all three domains, lives here.

Design notes:
  * Plain HTTP handlers, no framework lock-in. Runs under uvicorn if present,
    otherwise stdlib http.server, so the service never fails to start because
    of a missing dependency.
  * The DB layer is Supabase's REST API over httpx/urllib. No ORM: the schema
    is small and a generated client would add a dependency for no gain.
  * Every handler returns JSON. Errors return a machine-readable `error` code
    so the app can react, rather than an HTML traceback.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

try:                                    # Python 3.9+
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("VECTOR_TZ", "Asia/Jakarta"))
except Exception:                       # pragma: no cover - tzdata missing
    LOCAL_TZ = timezone(timedelta(hours=7))

from decompose import decompose
import store

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
PORT = int(os.environ.get("PORT", "8790"))

# Shared-secret auth.
#
# WHY THIS EXISTS: the API previously trusted the X-User-Id header outright.
# That is acceptable on a Tailscale-private network, but the moment the service
# is reachable from the public internet (a tunnel, a public host) it means
# anyone who learns the URL can read the owner's finances and write tasks.
# So a secret is required as soon as VECTOR_API_KEY is set.
#
# Left unset, the service runs open -- which keeps local/Tailscale development
# working and makes the requirement explicit rather than silently insecure.
API_KEY = os.environ.get("VECTOR_API_KEY", "")


def _auth_ok(headers: dict) -> bool:
    """Constant-time compare so the check cannot be timed."""
    if not API_KEY:
        return True
    import hmac
    supplied = headers.get("X-Api-Key") or headers.get("x-api-key") or ""
    return hmac.compare_digest(supplied, API_KEY)


def _now_local_iso() -> str:
    """Timestamp written on completion.

    Stored in LOCAL time, not UTC: the product's unit of work is "today", and
    `today` is the user's calendar day. Storing UTC made every task finished
    between 00:00 and 07:00 WIB fall on the previous day and silently vanish
    from the day's completed list.
    """
    return datetime.now(LOCAL_TZ).isoformat()


def _today_local() -> str:
    """The user's current calendar date, as YYYY-MM-DD."""
    return datetime.now(LOCAL_TZ).date().isoformat()


def _days_ago_local(n: int) -> str:
    return (datetime.now(LOCAL_TZ).date() - timedelta(days=n)).isoformat()


def _db_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _supabase_request(method: str, path: str, *, params: dict | None = None,
                      body: Any = None, timeout: int = 30) -> Any:
    """Call the Supabase REST (PostgREST) API. Raises RuntimeError on failure."""
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_db_headers(),
                                method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "[]"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise RuntimeError(f"db_{e.code}: {detail}") from e


def db_request(method: str, path: str, *, params: dict | None = None,
               body: Any = None, timeout: int = 30) -> Any:
    """Storage dispatch.

    Supabase is the production target (private Postgres, RLS, multi-device).
    When it is not configured the identical call is served by a local SQLite
    store implementing the same PostgREST subset, so the product is usable
    before any cloud account exists. Handlers never know which is in play.
    """
    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        return _supabase_request(method, path, params=params, body=body,
                                 timeout=timeout)
    return store.db_request(method, path, params=params, body=body)


# --------------------------------------------------------------------------
# Domain logic
# --------------------------------------------------------------------------
def create_goal_with_tasks(user_id: str, title: str, detail: str | None = None,
                           target_date: str | None = None) -> dict[str, Any]:
    """The product's central action: one goal in, a startable plan out.

    The goal is inserted first so the user never loses what they typed, even
    if decomposition fails. A degraded plan is still returned and flagged --
    losing the goal would be worse than returning a thin plan.
    """
    goal_rows = db_request("POST", "goals", body={
        "user_id": user_id, "title": title, "detail": detail,
        "target_date": target_date,
    })
    goal = goal_rows[0] if isinstance(goal_rows, list) and goal_rows else goal_rows
    goal_id = goal["id"]

    result = decompose(title, detail)
    if not result.tasks:
        return {"goal": goal, "tasks": [], "degraded": True,
                "note": result.note}

    # Insert in dependency order, resolving index refs to real UUIDs as we go.
    id_by_order: dict[int, str] = {}
    inserted: list[dict] = []
    for t in result.tasks:
        blocked_uuid = id_by_order.get(t.blocked_by) if t.blocked_by is not None else None
        rows = db_request("POST", "tasks", body={
            "user_id": user_id,
            "goal_id": goal_id,
            "title": t.title,
            "why": t.why,
            "minutes": t.minutes,
            "blocked_by": blocked_uuid,
            "priority": 2 if t.order == 0 else 3,
            "source": "ai",
        })
        row = rows[0] if isinstance(rows, list) and rows else rows
        id_by_order[t.order] = row["id"]
        inserted.append(row)

    return {"goal": goal, "tasks": inserted, "degraded": result.degraded,
            "note": result.note}


def startable_tasks(user_id: str) -> list[dict]:
    """Only what can be started now -- the home screen's entire payload."""
    return db_request("GET", "startable_tasks", params={
        "user_id": f"eq.{user_id}",
        "order": "priority.asc,minutes.asc",
        "limit": "20",
    })


def today_plan(user_id: str) -> dict[str, Any]:
    """Assemble today: what's startable, what's scheduled, what's been done."""
    today = _today_local()
    todo = startable_tasks(user_id)
    done_today = db_request("GET", "tasks", params={
        "user_id": f"eq.{user_id}",
        "status": "eq.done",
        "completed_at": f"gte.{today}T00:00:00",
        "select": "id,title,minutes,completed_at",
    })
    sessions = db_request("GET", "focus_sessions", params={
        "user_id": f"eq.{user_id}",
        "started_at": f"gte.{today}T00:00:00",
        "select": "minutes,completed",
    })
    focus_minutes = sum((s.get("minutes") or 0) for s in sessions)
    return {
        "date": today,
        "startable": todo,
        "done_today": done_today,
        "focus_minutes": focus_minutes,
        "sessions": len(sessions),
    }


def productivity(user_id: str, days: int = 14) -> dict[str, Any]:
    """Planned-vs-actual over a window. Computed from logs, never a counter."""
    since = _days_ago_local(days)
    rows = db_request("GET", "daily_productivity", params={
        "user_id": f"eq.{user_id}",
        "day": f"gte.{since}",
        "order": "day.desc",
    })
    done = db_request("GET", "tasks", params={
        "user_id": f"eq.{user_id}",
        "status": "eq.done",
        "completed_at": f"gte.{since}T00:00:00",
        "select": "id,minutes,completed_at",
    })
    per_day: dict[str, int] = {}
    for d in done:
        day = (d.get("completed_at") or "")[:10]
        per_day[day] = per_day.get(day, 0) + 1
    streak = 0
    cursor = datetime.now(LOCAL_TZ).date()
    while per_day.get(cursor.isoformat(), 0) > 0:
        streak += 1
        cursor -= timedelta(days=1)
    return {
        "window_days": days,
        "daily": rows,
        "tasks_done": len(done),
        "minutes_focused": sum((r.get("minutes") or 0) for r in rows),
        "current_streak": streak,
    }


def finance_summary(user_id: str) -> dict[str, Any]:
    """Balance, burn rate and runway -- same maths as the existing planner."""
    settings = db_request("GET", "finance_settings", params={
        "user_id": f"eq.{user_id}", "limit": "1"})
    s = settings[0] if settings else {
        "balance": 0, "daily_budget": 0, "currency": "IDR", "target_date": None}

    since = _days_ago_local(30)
    expenses = db_request("GET", "expenses", params={
        "user_id": f"eq.{user_id}", "spent_on": f"gte.{since}",
        "select": "amount,spent_on"})

    total_spent = sum(float(e["amount"]) for e in expenses)
    days_with_spend = len({e["spent_on"] for e in expenses}) or 1
    avg_daily = total_spent / days_with_spend
    balance = float(s.get("balance") or 0)

    runway_days = int(balance / avg_daily) if avg_daily > 0 else None
    today_spent = sum(float(e["amount"]) for e in expenses
                      if e["spent_on"] == _today_local())
    daily_budget = float(s.get("daily_budget") or 0)

    return {
        "currency": s.get("currency", "IDR"),
        "balance": balance,
        "daily_budget": daily_budget,
        "avg_daily_spend": round(avg_daily, 2),
        "today_spent": today_spent,
        "free_today": round(max(0.0, daily_budget - today_spent), 2) if daily_budget else None,
        "runway_days": runway_days,
        "target_date": s.get("target_date"),
        "spent_30d": round(total_spent, 2),
    }


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
def _json_response(status: int, payload: Any) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json"}, json.dumps(payload).encode()


def route(method: str, path: str, body: dict, user_id: str | None,
          api_key: str | None = None) -> tuple[int, dict, bytes]:
    """Pure routing function -- testable without a live socket."""
    p = path.rstrip("/") or "/"

    if p == "/health" and method == "GET":
        # Deliberately unauthenticated: a health probe that needs a secret is
        # useless for uptime monitoring, and it exposes no user data.
        return _json_response(200, {
            "ok": True,
            "db": "supabase" if (SUPABASE_URL and SUPABASE_SERVICE_KEY)
                  else "local-sqlite",
            "auth": bool(API_KEY),
            "llm": bool(os.environ.get("LLM_API_KEY") or os.environ.get("LLM_MODEL")
                        or os.path.exists(os.path.expanduser("~/.hermes/config.yaml"))),
        })

    # Every data route requires the shared secret when one is configured.
    if not _auth_ok({"X-Api-Key": api_key or ""}):
        return _json_response(401, {"error": "unauthorized"})

    if not user_id:
        return _json_response(401, {"error": "missing_user"})

    try:
        if p == "/goals" and method == "POST":
            title = (body.get("title") or "").strip()
            if not title:
                return _json_response(400, {"error": "title_required"})
            return _json_response(201, create_goal_with_tasks(
                user_id, title, body.get("detail"), body.get("target_date")))

        if p == "/goals" and method == "GET":
            return _json_response(200, db_request("GET", "goal_progress", params={
                "user_id": f"eq.{user_id}", "order": "title.asc"}))

        if p == "/tasks/startable" and method == "GET":
            return _json_response(200, startable_tasks(user_id))

        if p == "/tasks" and method == "PATCH":
            task_id = body.get("id")
            if not task_id:
                return _json_response(400, {"error": "id_required"})
            patch: dict[str, Any] = {}
            if body.get("status"):
                patch["status"] = body["status"]
                if body["status"] == "done":
                    patch["completed_at"] = _now_local_iso()
            for k in ("title", "minutes", "priority", "scheduled_at"):
                if k in body:
                    patch[k] = body[k]
            if not patch:
                return _json_response(400, {"error": "nothing_to_update"})
            rows = db_request("PATCH", "tasks",
                              params={"id": f"eq.{task_id}", "user_id": f"eq.{user_id}"},
                              body=patch)
            return _json_response(200, rows)

        if p == "/today" and method == "GET":
            return _json_response(200, today_plan(user_id))

        if p == "/productivity" and method == "GET":
            return _json_response(200, productivity(user_id))

        if p == "/finance" and method == "GET":
            return _json_response(200, finance_summary(user_id))

        if p == "/expenses" and method == "POST":
            amt = body.get("amount")
            if amt is None or float(amt) <= 0:
                return _json_response(400, {"error": "amount_required"})
            rows = db_request("POST", "expenses", body={
                "user_id": user_id, "amount": float(amt),
                "note": body.get("note"), "category": body.get("category"),
            })
            return _json_response(201, rows)

        return _json_response(404, {"error": "not_found", "path": p})

    except RuntimeError as e:
        # Config / DB errors are the app's problem to surface, not a crash.
        return _json_response(502, {"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return _json_response(500, {"error": f"{type(e).__name__}: {e}"})


def build_wsgi_app():
    """WSGI callable so this runs under uvicorn/gunicorn or plain wsgiref."""

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        user_id = environ.get("HTTP_X_USER_ID")
        api_key = environ.get("HTTP_X_API_KEY")

        length = int(environ.get("CONTENT_LENGTH") or 0)
        body: dict = {}
        if length:
            try:
                body = json.loads(environ["wsgi.input"].read(length) or b"{}")
            except (json.JSONDecodeError, ValueError):
                status, headers, payload = _json_response(400, {"error": "bad_json"})
                start_response(f"{status} Bad Request", list(headers.items()))
                return [payload]

        status, headers, payload = route(method, path, body, user_id, api_key)
        start_response(f"{status} OK", list(headers.items()))
        return [payload]

    return app


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    app = build_wsgi_app()
    print(f"VECTOR Suite API on 0.0.0.0:{PORT}", flush=True)
    make_server("0.0.0.0", PORT, app).serve_forever()
