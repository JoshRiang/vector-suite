"""Fail when a `late final` field is assigned more than once.

WHY: a `late final` field may be assigned exactly once. A second assignment
throws LateInitializationError AT RUNTIME, asynchronously if it happens inside a
Future - so it does not crash the screen, it leaves the widget stuck in its
initial state. In vector-tasks that produced a permanently blank white screen
with no message, and no static check or test caught it. This is a compile-time
error to a human but legal Dart, so it needs an explicit check.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path("/home/josh/vector_suite")
APPS = ["vector-tasks", "vector-calendar", "vector-finance"]

# `late final <Type> _name;`  (also tolerates `late final _name` without a type)
DECL = re.compile(r"\blate\s+final\s+(?:[A-Za-z_][\w<>,\.\? ]*\s+)?(_?\w+)\s*;")


def check(path: Path) -> list[str]:
    errs: list[str] = []
    if not path.exists():
        return errs
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()

    # every `late final` field declared in this file
    declared = {}
    for n, line in enumerate(lines, 1):
        for m in DECL.finditer(line):
            declared.setdefault(m.group(1), n)

    if not declared:
        return errs

    # count assignments `name = ...` that are NOT the declaration itself.
    # Comparison operators (==, <=, >=, !=) must not count.
    for name, decl_line in declared.items():
        assigns = []
        pat = re.compile(rf"(?<![=!<>])\b{re.escape(name)}\s*=(?!=)")
        for n, line in enumerate(lines, 1):
            code = line.split("//")[0]
            if DECL.search(line):          # the declaration line itself
                continue
            if pat.search(code):
                assigns.append(n)
        if len(assigns) > 1:
            errs.append(
                f"{path.name}: `late final {name}` declared line {decl_line} "
                f"but assigned {len(assigns)}x (lines {assigns}) - the 2nd "
                f"assignment throws LateInitializationError at runtime")
    return errs


def main() -> int:
    all_errs: list[str] = []
    for app in APPS:
        errs = check(ROOT / app / "lib/main.dart")
        if errs:
            print(f"FAIL {app}")
            for e in errs:
                print(f"   - {e}")
            all_errs += errs
        else:
            print(f"OK   {app}")
    print()
    if all_errs:
        print(f"{len(all_errs)} late-final violation(s)")
        return 1
    print("NO LATE-FINAL DOUBLE ASSIGNMENTS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
