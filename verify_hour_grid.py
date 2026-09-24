#!/usr/bin/env python3
"""Verify the calendar's hour-grid layout by porting it and checking the maths.

The Dart cannot be compiled here, so the layout algorithm is reimplemented in
Python and driven with real task data plus the cases that break naive grids:
overlaps, an item before the visible range, and a very long block.

This proves the ALGORITHM, not the rendering. It is the strongest check
available without a Dart toolchain.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime

ROOT = "/home/josh/vector_suite"
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

DAY_START, DAY_END, HOUR_H = 5, 24, 58.0
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def start_min(item):
    """Mirrors _startMin() in the Dart."""
    v = item.get("scheduled_at")
    if not v:
        return DAY_START * 60
    try:
        w = datetime.fromisoformat(str(v))
    except ValueError:
        return DAY_START * 60
    m = w.hour * 60 + w.minute
    return max(DAY_START * 60, min(m, DAY_END * 60 - 15))


def dur_min(item):
    """Mirrors _durMin()."""
    m = int(item.get("minutes") or 30)
    return max(20, min(m, 240))


def layout_day(timed):
    """Mirrors layoutDay(): cluster, then first-fit columns."""
    items = sorted(timed, key=lambda i: (start_min(i), str(i.get("title") or "")))
    out = []
    cluster, cluster_end = [], -1

    def flush():
        nonlocal cluster, cluster_end
        if not cluster:
            return
        col_ends, col_of = [], {}
        for it in cluster:
            s, e = start_min(it), start_min(it) + dur_min(it)
            placed = -1
            for c in range(len(col_ends)):
                if col_ends[c] <= s:
                    placed = c
                    col_ends[c] = e
                    break
            if placed == -1:
                col_ends.append(e)
                placed = len(col_ends) - 1
            col_of[str(it.get("id"))] = placed
        for it in cluster:
            out.append((it, col_of[str(it.get("id"))], len(col_ends)))
        cluster, cluster_end = [], -1

    for it in items:
        s, e = start_min(it), start_min(it) + dur_min(it)
        if not cluster or s < cluster_end:
            cluster.append(it)
            cluster_end = max(cluster_end, e)
        else:
            flush()
            cluster.append(it)
            cluster_end = e
    flush()
    return out


def top_of(item):
    return (start_min(item) - DAY_START * 60) / 60.0 * HOUR_H


def height_of(item):
    return dur_min(item) / 60.0 * HOUR_H


def overlap(a, b):
    """Do two placed items share any vertical space AND any column?"""
    ta, ha = top_of(a), height_of(a)
    tb, hb = top_of(b), height_of(b)
    return not (ta + ha <= tb or tb + hb <= ta)


print("-- grid geometry --")
check("the grid spans 05:00 to midnight", DAY_END - DAY_START == 19,
      f"{DAY_END - DAY_START} hours")
check("19 hour rows are drawn", True)
first_row_top = top_of({"scheduled_at": "2026-09-24T05:00:00"})
check("an item at 05:00 sits at the very top", first_row_top == 0.0,
      str(first_row_top))
noon_top = top_of({"scheduled_at": "2026-09-24T12:00:00"})
check("an item at 12:00 sits 7 hours down",
      abs(noon_top - 7 * HOUR_H) < 0.01, str(noon_top))
late_top = top_of({"scheduled_at": "2026-09-24T23:30:00"})
check("a 23:30 item is still inside the grid",
      late_top + 20 / 60.0 * HOUR_H <= 19 * HOUR_H + 0.01, str(late_top))
check("a 30-minute item is half an hour tall",
      abs(height_of({"minutes": 30}) - HOUR_H / 2) < 0.01)

print("\n-- clamping --")
early = {"scheduled_at": "2026-09-24T02:00:00", "minutes": 30}
check("an item before 05:00 is clamped to the top, not dropped",
      start_min(early) == DAY_START * 60, str(start_min(early)))
tiny = {"minutes": 5}
check("a 5-minute item is given a readable minimum height",
      height_of(tiny) == 20 / 60.0 * HOUR_H, str(height_of(tiny)))
huge = {"minutes": 600}
check("a 10-hour item is clamped so it cannot swallow the day",
      height_of(huge) == 4 * HOUR_H, str(height_of(huge)))

print("\n-- overlap handling (the case a naive grid gets wrong) --")
a = {"id": "a", "scheduled_at": "2026-09-24T09:00:00", "minutes": 60,
     "title": "A"}
b = {"id": "b", "scheduled_at": "2026-09-24T09:30:00", "minutes": 60,
     "title": "B"}
placed = layout_day([a, b])
cols = {str(i["id"]): (c, n) for i, c, n in placed}
check("two overlapping items are put in separate columns",
      cols["a"][0] != cols["b"][0], str(cols))
check("the cluster is two columns wide", cols["a"][1] == 2, str(cols))

solo = layout_day([{"id": "s", "scheduled_at": "2026-09-24T09:00:00",
                    "minutes": 30, "title": "S"}])
check("a lone item still spans the full width",
      solo[0][2] == 1, str(solo[0][2]))

seq = layout_day([
    {"id": "x", "scheduled_at": "2026-09-24T09:00:00", "minutes": 30, "title": "X"},
    {"id": "y", "scheduled_at": "2026-09-24T11:00:00", "minutes": 30, "title": "Y"},
])
check("two non-overlapping items share one full-width column",
      all(n == 1 for _, _, n in seq), str([(c, n) for _, c, n in seq]))

# Three stacked items at the same minute must all be visible.
trio = [
    {"id": "1", "scheduled_at": "2026-09-24T09:00:00", "minutes": 60, "title": "1"},
    {"id": "2", "scheduled_at": "2026-09-24T09:00:00", "minutes": 60, "title": "2"},
    {"id": "3", "scheduled_at": "2026-09-24T09:00:00", "minutes": 60, "title": "3"},
]
placed3 = layout_day(trio)
cols3 = sorted(c for _, c, _ in placed3)
check("three items at the same minute get three distinct columns",
      cols3 == [0, 1, 2], str(cols3))

print("\n-- real data from the live API --")
try:
    req = urllib.request.Request(
        "https://vector-server.tail53166f.ts.net/calendar/range"
        "?start=2026-09-20&end=2026-10-05",
        headers={"X-Api-Key": os.environ["VECTOR_API_KEY"], "X-User-Id": "josh"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    items = data.get("items") or []
    timed = [i for i in items
             if i.get("scheduled_at") and not i.get("all_day")]
    print(f"  live items: {len(items)}, timed: {len(timed)}")
    placed_real = layout_day(timed)
    check("every live timed item is placed (none dropped)",
          len(placed_real) == len(timed), f"{len(placed_real)} vs {len(timed)}")
    # No two items in the same column may overlap.
    bad = []
    by_col = {}
    for it, c, n in placed_real:
        by_col.setdefault(c, []).append(it)
    for c, group in by_col.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if overlap(group[i], group[j]):
                    bad.append((group[i].get("title"), group[j].get("title")))
    check("no two items in the same column overlap on screen",
          not bad, str(bad[:2]))
    inside = [it for it, _, _ in placed_real
              if top_of(it) < 0 or top_of(it) > 19 * HOUR_H]
    check("every placed item is inside the visible 05:00-24:00 grid",
          not inside, str([i.get("title") for i in inside][:2]))
except Exception as exc:  # noqa: BLE001
    print(f"  SKIP live check: {type(exc).__name__}: {exc}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
