"""Tests for the decomposition engine's untrusted-output handling.

These run WITHOUT any network or LLM: they exercise the parse/coerce/repair
path directly, which is where real breakage happens. A model that returns
garbage must degrade, never crash and never emit a cyclic plan.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from decompose import (  # noqa: E402
    FIRST_TASK_MAX_MINUTES, MAX_TASKS, Task,
    _coerce_tasks, _extract_json, _repair,
)


class TestExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_extract_json('{"tasks":[]}'), {"tasks": []})

    def test_fenced(self):
        got = _extract_json('```json\n{"tasks":[]}\n```')
        self.assertEqual(got, {"tasks": []})

    def test_fenced_no_lang(self):
        self.assertEqual(_extract_json('```\n{"tasks":[]}\n```'), {"tasks": []})

    def test_chatty_prefix(self):
        got = _extract_json('Sure! Here is your plan:\n{"tasks":[]}\nHope it helps')
        self.assertEqual(got, {"tasks": []})

    def test_garbage(self):
        self.assertIsNone(_extract_json("I cannot help with that."))

    def test_empty(self):
        self.assertIsNone(_extract_json(""))

    def test_truncated_is_none(self):
        # A cut-off response must not half-parse into a phantom plan.
        self.assertIsNone(_extract_json('{"tasks":[{"title":"a"'))


class TestCoerce(unittest.TestCase):
    def test_basic(self):
        ts = _coerce_tasks({"tasks": [
            {"title": "A", "minutes": 20, "why": "w", "blocked_by": None},
            {"title": "B", "minutes": 40, "why": "w", "blocked_by": 0},
        ]})
        self.assertEqual([t.title for t in ts], ["A", "B"])
        self.assertIsNone(ts[0].blocked_by)
        self.assertEqual(ts[1].blocked_by, 0)

    def test_clamps_minutes(self):
        ts = _coerce_tasks({"tasks": [
            {"title": "A", "minutes": 99999},
            {"title": "B", "minutes": -5},
            {"title": "C", "minutes": "abc"},
        ]})
        self.assertEqual([t.minutes for t in ts], [480, 5, 30])

    def test_missing_title_dropped(self):
        ts = _coerce_tasks({"tasks": [
            {"title": "A"}, {"minutes": 10}, {"title": "   "},
        ]})
        self.assertEqual(len(ts), 1)

    def test_caps_task_count(self):
        ts = _coerce_tasks({"tasks": [{"title": f"T{i}"} for i in range(20)]})
        self.assertEqual(len(ts), MAX_TASKS)

    def test_forward_dependency_nulled(self):
        # Blocked by a LATER task is unsatisfiable -> must become unblocked.
        ts = _coerce_tasks({"tasks": [
            {"title": "A", "blocked_by": 1},
            {"title": "B", "blocked_by": None},
        ]})
        self.assertIsNone(ts[0].blocked_by)

    def test_self_dependency_nulled(self):
        ts = _coerce_tasks({"tasks": [{"title": "A", "blocked_by": 0}]})
        self.assertIsNone(ts[0].blocked_by)

    def test_out_of_range_dependency_nulled(self):
        ts = _coerce_tasks({"tasks": [
            {"title": "A"}, {"title": "B", "blocked_by": 99},
        ]})
        self.assertIsNone(ts[1].blocked_by)

    def test_tasks_not_a_list(self):
        self.assertEqual(_coerce_tasks({"tasks": "nope"}), [])
        self.assertEqual(_coerce_tasks({}), [])

    def test_non_dict_entries_skipped(self):
        ts = _coerce_tasks({"tasks": ["x", None, {"title": "ok"}]})
        self.assertEqual([t.title for t in ts], ["ok"])


class TestRepair(unittest.TestCase):
    def test_first_task_clamped(self):
        ts = _repair([Task("big", 300, order=0), Task("b", 30, order=1)])
        self.assertLessEqual(ts[0].minutes, FIRST_TASK_MAX_MINUTES)

    def test_first_never_blocked(self):
        ts = _repair([Task("a", 10, blocked_by=1, order=0),
                      Task("b", 10, order=1)])
        self.assertIsNone(ts[0].blocked_by)

    def test_duplicate_titles_dropped_and_remapped(self):
        # If task 0 is a duplicate of nothing but task 2 duplicates task 1,
        # dependents of the dropped task must be remapped, not left dangling.
        ts = _repair([
            Task("A", 10, order=0),
            Task("B", 10, order=1),
            Task("b", 10, order=2),          # duplicate of B -> dropped
            Task("C", 10, blocked_by=2, order=3),
        ])
        titles = [t.title for t in ts]
        self.assertEqual(titles, ["A", "B", "C"])
        # C's blocker index 2 must now point at the surviving B (index 1).
        self.assertEqual(ts[2].blocked_by, 1)

    def test_orders_are_sequential(self):
        ts = _repair([Task(f"T{i}", 10, order=i) for i in range(5)])
        self.assertEqual([t.order for t in ts], [0, 1, 2, 3, 4])

    def test_no_cycles_after_repair(self):
        # Every dependency must point strictly backwards, which makes a cycle
        # structurally impossible.
        ts = _repair([Task(f"T{i}", 10, blocked_by=max(0, i - 1), order=i)
                      for i in range(6)])
        for i, t in enumerate(ts):
            if t.blocked_by is not None:
                self.assertLess(t.blocked_by, i)

    def test_empty(self):
        self.assertEqual(_repair([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
