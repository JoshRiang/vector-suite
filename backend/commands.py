"""Turn a natural-language instruction into real calendar/task changes.

DESIGN
------
This is NOT a chatbot. The user types an instruction ("move my 3pm to tomorrow",
"add gym Friday 7am for an hour", "what does Thursday look like?") and it is
turned into concrete operations against the same tables the calendar and task
apps read. The reply is a report of what actually changed.

Safety properties, in order of importance:
  1. The model never emits SQL. It emits a small JSON vocabulary of operations,
     and every field is validated against the writable-column allowlist before
     anything touches the store. An unknown action or field is rejected.
  2. Dates are re-validated here, not trusted. A relative date ("tomorrow") is
     resolved against the server's real local date, and a date outside a sane
     window is refused rather than written.
  3. Every instruction and its outcome is logged, so a wrong change is
     traceable and reversible by hand.
  4. A failed model call never loses the instruction: it is stored and the
     caller gets a plain "could not understand" rather than a silent no-op.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

import store

# Actions the model may request. Anything else is rejected outright.
ACTIONS = {
    "create_task", "update_task", "delete_task",
    "create_goal", "update_goal", "complete_task", "reopen_task",
    "answer",   # a question with no mutation, e.g. "what does Thursday look like"
}

# How far ahead/behind a date may be before it is treated as a misparse.
MAX_DAYS_AHEAD = 730
MAX_DAYS_BEHIND = 365

SYSTEM_PROMPT = """You convert one instruction about a calendar/task list into JSON operations.

Return ONLY JSON, no prose, in this exact shape:
{"ops": [ ... ], "reply": "one short sentence describing what you did"}

Allowed ops (use only these fields):
  {"action":"create_task","title":str,"scheduled_at":"YYYY-MM-DDTHH:MM:SS","minutes":int,
   "all_day":bool,"location":str,"notes":str,"goal_id":str,"priority":1-5,"color":str}
  {"action":"update_task","id":str,"title":str,"scheduled_at":str,"minutes":int,
   "all_day":bool,"location":str,"notes":str,"status":"todo"|"doing"|"done"}
  {"action":"delete_task","id":str}
  {"action":"complete_task","id":str}
  {"action":"reopen_task","id":str}
  {"action":"create_goal","title":str,"detail":str,"target_date":"YYYY-MM-DD"}
  {"action":"update_goal","id":str,"title":str,"target_date":str,"status":str}
  {"action":"answer"}   // nothing to change; the "reply" field carries the answer

Rules:
- Omit fields you are not changing. Never invent ids: use only ids from the
  CONTEXT block below.
- Resolve every relative date ("tomorrow", "next Friday") to an absolute
  YYYY-MM-DD using TODAY from the context. Never output a relative date.
- An instruction that names an existing item by time or title should update or
  complete THAT item, using its id from the context, rather than creating a new one.
- If the instruction asks a question or is unclear, use the "answer" action and
  put a short helpful reply in "reply". Never guess a destructive change.
- Keep "reply" under 140 characters, plain and concrete.
"""


def _llm_config() -> tuple[str, str, str]:
    base = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if not (base and key and model):
        raise RuntimeError("no LLM configured (LLM_BASE_URL/LLM_API_KEY/LLM_MODEL)")
    return base, key, model


def _now_local() -> datetime:
    import api
    return datetime.now(api.LOCAL_TZ)


def _context(user_id: str, limit: int = 40) -> str:
    """A compact view of what exists, so the model can resolve references."""
    import api
    today = _now_local().date().isoformat()
    tasks = store.db_request("GET", "tasks", params={
        "user_id": f"eq.{user_id}",
        "select": "id,title,scheduled_at,status,minutes,goal_id",
        "limit": str(limit),
    }) or []
    goals = store.db_request("GET", "goals", params={
        "user_id": f"eq.{user_id}",
        "select": "id,title,status,target_date",
        "limit": "20",
    }) or []
    lines = [f"TODAY: {today}", f"NOW: {_now_local().strftime('%Y-%m-%dT%H:%M:%S')}", ""]
    lines.append("GOALS:")
    for g in goals:
        lines.append(f"  {g.get('id')} | {g.get('title')} | {g.get('status')}")
    lines.append("")
    lines.append("TASKS:")
    for t in tasks:
        lines.append(
            f"  {t.get('id')} | {t.get('title')} | sched={t.get('scheduled_at')} "
            f"| {t.get('status')} | {t.get('minutes')}min")
    return "\n".join(lines)


def _call_llm(instruction: str, context: str, timeout: int = 90) -> str:
    base, key, model = _llm_config()
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"CONTEXT:\n{context}\n\nINSTRUCTION: {instruction}"},
        ],
        "temperature": 0.1,
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


def _extract_json(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw[3:]
        raw = raw.lstrip("json").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost braces, which tolerates a chatty model.
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _valid_datetime(value: Any) -> str | None:
    """Accept a real date/datetime and reject anything outside a sane window."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    today = _now_local().date()
    delta = (parsed.date() - today).days
    if delta > MAX_DAYS_AHEAD or delta < -MAX_DAYS_BEHIND:
        return None
    # A date with no time means all-day; keep the same storage shape.
    if len(text) == 10:
        return f"{text}T00:00:00"
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _clean_text(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _clean_int(value: Any, lo: int, hi: int) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _apply_op(user_id: str, op: dict) -> dict:
    """Validate and apply ONE operation. Returns a result record."""
    import api
    action = op.get("action")
    if action not in ACTIONS:
        return {"ok": False, "action": action, "error": "unknown_action"}

    if action == "answer":
        return {"ok": True, "action": action, "detail": "no change"}

    if action in ("complete_task", "reopen_task", "delete_task"):
        task_id = _clean_text(op.get("id"))
        if not task_id:
            return {"ok": False, "action": action, "error": "id_required"}
        # Scope by user so an id from another account cannot be touched.
        rows = store.db_request("GET", "tasks", params={
            "id": f"eq.{task_id}", "user_id": f"eq.{user_id}"}) or []
        if not rows:
            return {"ok": False, "action": action, "error": "task_not_found"}
        if action == "delete_task":
            store.db_request("DELETE", "tasks",
                             params={"id": f"eq.{task_id}",
                                     "user_id": f"eq.{user_id}"})
            return {"ok": True, "action": action, "id": task_id,
                    "title": rows[0].get("title")}
        patch = ({"status": "done", "completed_at": api._now_local_iso()}
                 if action == "complete_task"
                 else {"status": "todo", "completed_at": None})
        store.db_request("PATCH", "tasks",
                         params={"id": f"eq.{task_id}",
                                 "user_id": f"eq.{user_id}"}, body=patch)
        return {"ok": True, "action": action, "id": task_id,
                "title": rows[0].get("title")}

    if action in ("create_task", "update_task"):
        patch: dict[str, Any] = {}
        for field, limit in (("title", 200), ("location", 200),
                             ("notes", 2000), ("color", 20),
                             ("goal_id", 64)):
            val = _clean_text(op.get(field), limit)
            if val is not None:
                patch[field] = val
        if "scheduled_at" in op:
            when = _valid_datetime(op.get("scheduled_at"))
            if when is None:
                return {"ok": False, "action": action,
                        "error": "bad_scheduled_at"}
            patch["scheduled_at"] = when
        if "minutes" in op:
            mins = _clean_int(op.get("minutes"), 1, 24 * 60)
            if mins is not None:
                patch["minutes"] = mins
        if "priority" in op:
            pri = _clean_int(op.get("priority"), 1, 5)
            if pri is not None:
                patch["priority"] = pri
        if "all_day" in op:
            patch["all_day"] = 1 if op.get("all_day") else 0
        if "status" in op and op.get("status") in ("todo", "doing", "done"):
            patch["status"] = op["status"]
            if op["status"] == "done":
                patch["completed_at"] = api._now_local_iso()

        if action == "create_task":
            if not patch.get("title"):
                return {"ok": False, "action": action, "error": "title_required"}
            # An all-day item carries a date, not a time.
            if patch.get("all_day") and patch.get("scheduled_at"):
                patch["scheduled_at"] = patch["scheduled_at"][:10] + "T00:00:00"
            patch.setdefault("status", "todo")
            patch.setdefault("minutes", 30)
            patch.setdefault("priority", 3)
            patch["source"] = "user"
            row = store.db_request("POST", "tasks",
                                   body={**patch, "user_id": user_id})
            if isinstance(row, list):
                row = row[0] if row else {}
            return {"ok": True, "action": action, "id": row.get("id"),
                    "title": patch.get("title"),
                    "scheduled_at": patch.get("scheduled_at")}

        task_id = _clean_text(op.get("id"))
        if not task_id:
            return {"ok": False, "action": action, "error": "id_required"}
        if not patch:
            return {"ok": False, "action": action, "error": "nothing_to_update"}
        rows = store.db_request("GET", "tasks", params={
            "id": f"eq.{task_id}", "user_id": f"eq.{user_id}"}) or []
        if not rows:
            return {"ok": False, "action": action, "error": "task_not_found"}
        store.db_request("PATCH", "tasks",
                         params={"id": f"eq.{task_id}",
                                 "user_id": f"eq.{user_id}"}, body=patch)
        return {"ok": True, "action": action, "id": task_id,
                "title": patch.get("title") or rows[0].get("title"),
                "scheduled_at": patch.get("scheduled_at")}

    if action in ("create_goal", "update_goal"):
        patch = {}
        title = _clean_text(op.get("title"), 200)
        detail = _clean_text(op.get("detail"), 2000)
        if title:
            patch["title"] = title
        if detail:
            patch["detail"] = detail
        if "target_date" in op:
            when = _valid_datetime(op.get("target_date"))
            if when is None:
                return {"ok": False, "action": action, "error": "bad_target_date"}
            patch["target_date"] = when[:10]
        if "status" in op and op.get("status") in ("active", "done", "paused"):
            patch["status"] = op["status"]

        if action == "create_goal":
            if not patch.get("title"):
                return {"ok": False, "action": action, "error": "title_required"}
            row = store.db_request("POST", "goals",
                                   body={**patch, "user_id": user_id,
                                         "status": patch.get("status", "active")})
            if isinstance(row, list):
                row = row[0] if row else {}
            return {"ok": True, "action": action, "id": row.get("id"),
                    "title": patch.get("title")}

        goal_id = _clean_text(op.get("id"))
        if not goal_id:
            return {"ok": False, "action": action, "error": "id_required"}
        if not patch:
            return {"ok": False, "action": action, "error": "nothing_to_update"}
        rows = store.db_request("GET", "goals", params={
            "id": f"eq.{goal_id}", "user_id": f"eq.{user_id}"}) or []
        if not rows:
            return {"ok": False, "action": action, "error": "goal_not_found"}
        store.db_request("PATCH", "goals",
                         params={"id": f"eq.{goal_id}",
                                 "user_id": f"eq.{user_id}"}, body=patch)
        return {"ok": True, "action": action, "id": goal_id,
                "title": patch.get("title") or rows[0].get("title")}

    return {"ok": False, "action": action, "error": "unhandled"}


def handle(user_id: str, instruction: str) -> dict:
    """Run one instruction end to end and report what actually changed."""
    import api
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "empty_instruction", "applied": []}

    store.db_request("POST", "chat_messages",
                     body={"user_id": user_id, "role": "user",
                           "content": instruction})

    try:
        raw = _call_llm(instruction, _context(user_id))
    except Exception as exc:  # noqa: BLE001
        # The instruction is already logged; say so plainly instead of pretending.
        reply = f"I could not reach my planner ({type(exc).__name__}). Nothing changed."
        store.db_request("POST", "chat_messages",
                         body={"user_id": user_id, "role": "assistant",
                               "content": reply})
        return {"ok": False, "error": "llm_unreachable", "reply": reply,
                "applied": []}

    payload = _extract_json(raw)
    # A bare JSON string or array parses successfully but is not the expected
    # object; treat it as unparseable rather than crashing on .get().
    if not isinstance(payload, dict):
        reply = "I could not turn that into an action. Try naming a task and a time."
        store.db_request("POST", "chat_messages",
                         body={"user_id": user_id, "role": "assistant",
                               "content": reply})
        return {"ok": False, "error": "unparseable", "reply": reply, "applied": []}

    ops = payload.get("ops")
    if not isinstance(ops, list):
        ops = []
    results = [_apply_op(user_id, op) for op in ops if isinstance(op, dict)]

    ok_count = sum(1 for r in results if r.get("ok"))
    failed = [r for r in results if not r.get("ok")]
    reply = _clean_text(payload.get("reply"), 300) or ""

    if not results:
        reply = reply or "Nothing to change."
    elif failed and not ok_count:
        # Report the real reason rather than a cheerful summary of nothing.
        reason = ", ".join(sorted({str(r.get("error")) for r in failed}))
        reply = f"I could not apply that ({reason}). Nothing changed."
    elif failed:
        reply = (reply or f"Applied {ok_count} change(s).") + \
            f" ({len(failed)} could not be applied)"

    store.db_request("POST", "chat_messages",
                     body={"user_id": user_id, "role": "assistant",
                           "content": reply})
    return {"ok": bool(ok_count) or not failed, "reply": reply,
            "applied": results, "count": ok_count}


def history(user_id: str, limit: int = 50) -> list[dict]:
    rows = store.db_request("GET", "chat_messages", params={
        "user_id": f"eq.{user_id}",
        "order": "created_at.asc",
        "limit": str(limit),
        "select": "id,role,content,created_at",
    }) or []
    return rows
