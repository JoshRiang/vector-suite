#!/usr/bin/env python3
"""Verify the widget's calendar grid math against a trusted implementation.

The Kotlin runs only on-device, so this ports the exact algorithm and compares
it to Python's `calendar` module for many months -- including leap years and
months that need 6 rows. It also checks the layout's header labels match the
week order the math assumes (Sunday first).
"""
import calendar
import pathlib
import re

# --- port of the Kotlin -------------------------------------------------------
# val lead = (first.get(Calendar.DAY_OF_WEEK) + 6) % 7
#
# Java's Calendar numbers weekdays SUNDAY=1, MONDAY=2 ... SATURDAY=7, whereas
# Python's calendar.monthrange gives Monday=0 ... Sunday=6. Translating with the
# ISO convention (Monday=1) instead of Java's made every day land one column too
# far left, which looked like a widget bug. This is the Java mapping.
JAVA_DOW = {0: 2, 1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 1}  # Mon=0 .. Sun=6


def widget_grid(year: int, month: int):
    """month is 1-based. Returns the 42-cell grid the widget would draw."""
    first_weekday_mon0 = calendar.monthrange(year, month)[0]
    java_dow = JAVA_DOW[first_weekday_mon0]
    lead = (java_dow + 6) % 7
    dim = calendar.monthrange(year, month)[1]
    return [i - lead + 1 if 1 <= i - lead + 1 <= dim else 0 for i in range(42)]


def expected_grid(year: int, month: int, sunday_first: bool = True):
    dim = calendar.monthrange(year, month)[1]
    cal = calendar.Calendar(firstweekday=6 if sunday_first else 0)
    weeks = cal.monthdayscalendar(year, month)
    flat = [d for w in weeks for d in w]
    return (flat + [0] * 42)[:42]


fails = 0
checked = 0
print("month-by-month comparison (Sunday-first)")
for year in (2024, 2025, 2026, 2027, 2028):
    for month in range(1, 13):
        got = widget_grid(year, month)
        want = expected_grid(year, month, sunday_first=True)
        checked += 1
        if got != want:
            fails += 1
            if fails <= 5:
                print(f"  MISMATCH {year}-{month:02d}")
                print(f"    widget:   {got}")
                print(f"    expected: {want}")
print(f"  {checked} months checked, {fails} mismatches")

# --- specific facts that must hold -------------------------------------------
print("\nspot checks")
cases = [
    (2026, 9, 23, "today per the session context (Wed)"),
    (2026, 1, 1, "New Year"),
    (2024, 2, 29, "leap day"),
    (2026, 2, 28, "non-leap Feb end"),
    (2026, 12, 31, "year end"),
]
for y, m, d, label in cases:
    grid = widget_grid(y, m)
    idx = grid.index(d)
    row, col = divmod(idx, 7)
    # Column 0 must be Sunday.
    first_mon0 = calendar.monthrange(y, m)[0]
    col_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    # The true weekday of day d:
    true_wd = calendar.weekday(y, m, d)          # Mon=0
    true_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][true_wd]
    ok = col_names[col] == true_name
    if not ok:
        fails += 1
    print(f"  {y}-{m:02d}-{d:02d} ({label}): cell {idx} row {row} col {col} "
          f"-> {col_names[col]}, actual {true_name}  {'ok' if ok else 'MISMATCH'}")

# --- does the layout's header agree? -----------------------------------------
print("\nlayout header vs the Sunday-first assumption")
lay = pathlib.Path(
    "vector-calendar/android/app/src/main/res/layout/widget_today.xml").read_text()
labels = re.findall(r'android:text="([A-Za-z])"', lay)
print(f"  header labels found: {labels}")
want_labels = ["S", "M", "T", "W", "T", "F", "S"]
if labels[:7] == want_labels:
    print("  ok   header is S M T W T F S (Sunday first), matching the grid math")
else:
    print(f"  MISMATCH header {labels[:7]} != {want_labels}")
    print("  (a Monday-first header with a Sunday-first grid mislabels every day)")
    fails += 1

print(f"\n{'ALL GRID CHECKS PASSED' if fails == 0 else str(fails) + ' FAILURES'}")
raise SystemExit(1 if fails else 0)
