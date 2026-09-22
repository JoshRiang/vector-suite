"""API routing tests -- run with no network, no Supabase, no LLM.

The routing function is pure, so the whole request/response contract is
verifiable offline. Anything that would touch the network is stubbed.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api  # noqa: E402


def call(method, path, body=None, user="u1"):
    status, headers, payload = api.route(method, path, body or {}, user)
    return status, json.loads(payload)


class TestAuth(unittest.TestCase):
    def test_health_needs_no_user(self):
        s, _ = call("GET", "/health", user=None)
        self.assertEqual(s, 200)

    def test_other_routes_require_user(self):
        for path in ("/today", "/goals", "/tasks/startable", "/finance"):
            s, b = call("GET", path, user=None)
            self.assertEqual(s, 401, path)
            self.assertEqual(b["error"], "missing_user")


class TestValidation(unittest.TestCase):
    def test_goal_requires_title(self):
        s, b = call("POST", "/goals", {"title": "   "})
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "title_required")

    def test_patch_requires_id(self):
        s, b = call("PATCH", "/tasks", {"status": "done"})
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "id_required")

    def test_patch_requires_a_field(self):
        s, b = call("PATCH", "/tasks", {"id": "t1"})
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "nothing_to_update")

    def test_expense_rejects_nonpositive(self):
        for amt in (0, -5):
            s, b = call("POST", "/expenses", {"amount": amt})
            self.assertEqual(s, 400, amt)
            self.assertEqual(b["error"], "amount_required")

    def test_unknown_route_404(self):
        s, b = call("GET", "/nope")
        self.assertEqual(s, 404)
        self.assertEqual(b["error"], "not_found")


class TestDbFailureIsSurfaced(unittest.TestCase):
    """A DB error must become a readable 502, never an unhandled crash."""

    def setUp(self):
        self._orig = api.db_request
        def boom(*a, **k):
            raise RuntimeError("db_500: relation does not exist")
        api.db_request = boom

    def tearDown(self):
        api.db_request = self._orig

    def test_502_on_db_error(self):
        for method, path in (("GET", "/today"), ("GET", "/finance"),
                             ("GET", "/tasks/startable")):
            s, b = call(method, path)
            self.assertEqual(s, 502, path)
            self.assertIn("db_500", b["error"])


class TestGoalCreation(unittest.TestCase):
    """The central action: one goal in, an ordered plan out with real UUIDs."""

    def setUp(self):
        self._orig = api.db_request
        self.calls = []
        self._seq = iter(f"id-{i}" for i in range(1, 40))

        def fake(method, path, *, params=None, body=None, timeout=30):
            self.calls.append((method, path, body))
            if path == "goals":
                return [{**body, "id": "goal-1"}]
            if path == "tasks":
                return [{**body, "id": next(self._seq)}]
            return []

        api.db_request = fake

        # Deterministic stand-in for the model.
        import decompose
        self._orig_dec = decompose.decompose
        def fake_dec(goal, detail=None):
            return decompose.Decomposition(tasks=[
                decompose.Task("A", 20, "why", None, 0),
                decompose.Task("B", 40, "why", 0, 1),
                decompose.Task("C", 30, "why", 1, 2),
            ])
        decompose.decompose = fake_dec
        api.decompose = fake_dec

    def tearDown(self):
        api.db_request = self._orig
        import decompose
        decompose.decompose = self._orig_dec

    def test_creates_goal_then_tasks_with_resolved_blockers(self):
        s, b = call("POST", "/goals", {"title": "Ship the thing"})
        self.assertEqual(s, 201)
        self.assertEqual(b["goal"]["id"], "goal-1")
        self.assertEqual(len(b["tasks"]), 3)
        self.assertFalse(b["degraded"])

        task_inserts = [c for c in self.calls if c[1] == "tasks"]
        self.assertEqual(len(task_inserts), 3)
        # Task 0 unblocked; task 1 blocked by task 0's REAL uuid, not index 0.
        self.assertIsNone(task_inserts[0][2]["blocked_by"])
        self.assertEqual(task_inserts[1][2]["blocked_by"], "id-1")
        self.assertEqual(task_inserts[2][2]["blocked_by"], "id-2")
        # Every task carries the goal id and the caller's user id.
        for _, _, body in task_inserts:
            self.assertEqual(body["goal_id"], "goal-1")
            self.assertEqual(body["user_id"], "u1")

    def test_goal_survives_decomposition_failure(self):
        # Even if the model returns nothing usable, the user's goal is kept.
        import decompose
        api.decompose = lambda g, d=None: decompose.Decomposition(
            tasks=[], degraded=True, note="llm_unreachable")
        s, b = call("POST", "/goals", {"title": "Something hard"})
        self.assertEqual(s, 201)
        self.assertEqual(b["goal"]["id"], "goal-1")
        self.assertEqual(b["tasks"], [])
        self.assertTrue(b["degraded"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
