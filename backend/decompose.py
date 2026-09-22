"""
Goal decomposition engine.

THE CORE PRODUCT BET
--------------------
The user's stated problem is not a lack of to-do lists -- it is that a list of
20 undifferentiated items produces paralysis and nothing gets started. So the
engine must do three things a plain list cannot:

  1. Order work so exactly one task is startable.
  2. Make the first task trivially small (< 30 min) to beat activation friction.
  3. Never return more than the user can hold in view at once.

The model is treated as an untrusted component: its output is parsed, validated,
and repaired before it ever reaches the database. A malformed response degrades
to a safe single task rather than an exception, because a broken assistant is
worse than a simple one.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Config -- read at call time, never at import time, so tests can override.
# --------------------------------------------------------------------------
MAX_TASKS = 7
MIN_TASKS = 3
FIRST_TASK_MAX_MINUTES = 30
MIN_MINUTES, MAX_MINUTES = 5, 480


def _llm_config() -> tuple[str, str, str]:
    """(base_url, api_key, model) from env, falling back to Hermes config."""
    base = os.environ.get("LLM_BASE_URL")
    key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if base and key and model:
        return base, key, model

    # Reuse the host's Hermes model config when the service runs on that box.
    try:
        import yaml  # noqa: PLC0415
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        cfg = yaml.safe_load(open(cfg_path))
        m = cfg["model"]
        return (
            base or m["base_url"],
            key or m["api_key"],
            model or m["default"],
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "No LLM configured. Set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL."
        ) from exc


SYSTEM_PROMPT = """You are a goal-decomposition engine inside a personal \
assistant app. The user states ONE end goal. You return the smallest ordered \
set of tasks that gets them there.

Return ONLY valid JSON, no prose, in exactly this shape:
{"tasks":[{"title":str,"minutes":int,"why":str,"blocked_by":int|null}]}

HARD RULES:
1. Between 3 and 7 tasks. Fewer is better. Never more than 7.
2. Task 0 MUST be completable in under 30 minutes. Its only purpose is to
   create momentum -- make it a concrete first physical action, never
   "research" or "plan" or "think about".
3. "blocked_by" is the 0-based index of the task that must finish first, or
   null. Task 0 must always have blocked_by: null.
4. "minutes" is a realistic single-sitting duration between 5 and 480.
5. "why" is one short clause explaining what this unblocks. Max 12 words.
6. Each title must be a concrete action a person can start without asking a
   follow-up question. "Set up repo and deploy a placeholder page" is good.
   "Work on the project" is forbidden.
7. Never include a task the user has clearly already done, based on the goal
   text."""


@dataclass
class Task:
    title: str
    minutes: int
    why: str = ""
    blocked_by: int | None = None
    order: int = 0

    def as_row(self, goal_id: str | None = None) -> dict[str, Any]:
        return {
            "title": self.title,
            "minutes": self.minutes,
            "why": self.why,
            "order": self.order,
            "blocked_by_order": self.blocked_by,
        }


@dataclass
class Decomposition:
    tasks: list[Task] = field(default_factory=list)
    degraded: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tasks": [
                {
                    "title": t.title,
                    "minutes": t.minutes,
                    "why": t.why,
                    "order": t.order,
                    "blocked_by": t.blocked_by,
                }
                for t in self.tasks
            ],
            "degraded": self.degraded,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# Parsing -- the model is untrusted, so every field is coerced and clamped.
# --------------------------------------------------------------------------
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the JSON object out of a possibly-fenced / chatty response."""
    if not text:
        return None
    cleaned = text.strip()
    # Strip a ```json ... ``` fence if present.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Last resort: grab the outermost {...}.
    m = _JSON_BLOCK.search(cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _coerce_tasks(payload: dict[str, Any]) -> list[Task]:
    raw = payload.get("tasks")
    if not isinstance(raw, list):
        return []

    out: list[Task] = []
    for i, item in enumerate(raw[:MAX_TASKS]):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue

        try:
            minutes = int(float(item.get("minutes") or 30))
        except (TypeError, ValueError):
            minutes = 30
        minutes = max(MIN_MINUTES, min(MAX_MINUTES, minutes))

        why = str(item.get("why") or "").strip()[:120]

        dep = item.get("blocked_by")
        if dep is None or isinstance(dep, bool):
            blocked_by = None
        else:
            try:
                dep_i = int(dep)
            except (TypeError, ValueError):
                dep_i = None
            # A dependency must point backwards and stay in range; anything
            # else (forward ref, self ref, out of range) becomes unblocked
            # rather than creating a cycle that can never be started.
            blocked_by = dep_i if (dep_i is not None and 0 <= dep_i < i) else None

        out.append(Task(title=title, minutes=minutes, why=why,
                        blocked_by=blocked_by, order=i))

    # Task 0 must never be blocked.
    if out:
        out[0].blocked_by = None
    return out


def _repair(tasks: list[Task]) -> list[Task]:
    """Enforce the invariants the UI depends on."""
    if not tasks:
        return tasks

    # The first task exists to beat activation friction. If the model made it
    # long, split nothing -- just clamp it and let the user start.
    if tasks[0].minutes > FIRST_TASK_MAX_MINUTES:
        tasks[0].minutes = FIRST_TASK_MAX_MINUTES

    # Drop duplicate titles (the model does this occasionally).
    # When a duplicate is dropped, its dependents must follow through to the
    # SURVIVING twin rather than losing their blocker -- otherwise the plan
    # silently parallelises work that was meant to be sequential.
    seen: dict[str, int] = {}   # lowercase title -> new index of the survivor
    remap: dict[int, int] = {}  # original order -> new index (or survivor's)
    deduped: list[Task] = []
    for t in tasks:
        k = t.title.lower()
        if k in seen:
            remap[t.order] = seen[k]
            continue
        seen[k] = len(deduped)
        remap[t.order] = len(deduped)
        deduped.append(t)

    for i, t in enumerate(deduped):
        t.order = i
        if t.blocked_by is not None:
            t.blocked_by = remap.get(t.blocked_by)
            if t.blocked_by is not None and t.blocked_by >= i:
                t.blocked_by = None
    deduped[0].blocked_by = None
    return deduped


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------
def _call_llm(goal: str, detail: str | None, timeout: int = 120) -> str:
    base, key, model = _llm_config()
    user = goal if not detail else f"{goal}\n\nExtra context: {detail}"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def decompose(goal: str, detail: str | None = None) -> Decomposition:
    """Turn one end goal into an ordered, startable task list.

    Never raises on a bad model response: an unusable answer degrades to a
    single safe first task so the user can still start moving.
    """
    goal = (goal or "").strip()
    if not goal:
        return Decomposition(tasks=[], degraded=True, note="empty goal")

    try:
        raw = _call_llm(goal, detail)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return Decomposition(
            tasks=[Task(title=f"Write down what '{goal[:60]}' means to you "
                              "in one sentence", minutes=10,
                        why="Clarifies the target before planning")],
            degraded=True,
            note=f"llm_unreachable: {type(exc).__name__}",
        )

    payload = _extract_json(raw)
    tasks = _repair(_coerce_tasks(payload or {}))

    if len(tasks) < MIN_TASKS:
        # A 1-2 task answer is not a usable plan; keep what we got but flag it
        # so the UI can offer a regenerate instead of pretending it is fine.
        return Decomposition(tasks=tasks, degraded=True,
                             note="too_few_tasks")

    return Decomposition(tasks=tasks, degraded=False, note="ok")
