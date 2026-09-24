#!/usr/bin/env python3
"""Independent static verification of the Dart apps.

Neither Flutter nor Dart exists on this machine, so CI is the only real
compiler. These checks catch, in the working tree, the specific classes of
error that have already shipped a blank screen -- they are not a substitute for
CI, they just fail earlier and cheaper.

Deliberately reimplemented rather than calling the existing checkers, so that a
bug in those does not hide a bug here.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path("/home/josh/vector_suite")
APPS = ["vector-tasks", "vector-calendar", "vector-finance"]


def strip_comments(src: str) -> str:
    """Remove // and /* */ comments, preserving strings roughly.

    A naive grep for `Colors.` or `late final` hits comments and class names
    like AppColors, which is how a checker ends up either crying wolf or being
    silenced. Dropping comments first makes the remaining matches real.
    """
    out = []
    i, n = 0, len(src)
    in_line = in_block = False
    quote = None
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            i += 1
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def balanced(src: str) -> tuple[bool, str]:
    depth = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    for ch in src:
        if ch in depth:
            depth[ch] += 1
        elif ch in pairs:
            depth[pairs[ch]] -= 1
            if depth[pairs[ch]] < 0:
                return False, f"unbalanced '{ch}'"
    bad = {k: v for k, v in depth.items() if v}
    return (not bad), (f"unclosed {bad}" if bad else "")


def guarded_casts(code: str, kind: str) -> list[str]:
    """Find `as <kind>` casts that are NOT narrowed by an `is` check.

    `x is num ? x as num : null` is provably safe: the `is` test has already
    narrowed the type, so the cast cannot throw. Only an unguarded cast can
    blank a screen, and flagging the guarded form would train the reader to
    ignore this check.
    """
    out = []
    for m in re.finditer(rf"\bas\s+{kind}\b", code):
        window = code[max(0, m.start() - 90):m.start()]
        if re.search(rf"\bis\s+{kind}\b", window):
            continue  # narrowed by a preceding is-check
        out.append(m.group(0))
    return out


def class_bodies(code: str) -> list[tuple[str, int, int]]:
    """Return (class_name, body_start, body_end) for each top-level class."""
    out = []
    for m in re.finditer(r"^class\s+(\w+)", code, re.M):
        start = code.find("{", m.end())
        if start == -1:
            continue
        depth = 0
        i = start
        while i < len(code):
            if code[i] == "{":
                depth += 1
            elif code[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((m.group(1), start, i))
    return out


def misplaced_members(code: str) -> list[str]:
    """Find class-scoped `static const` names used OUTSIDE their own class.

    This is a real CI failure that cost a build: `static const _noListId` was
    declared in _HomePageState but referenced from _TaskDetailPageState, which
    Dart rejects with "The getter '_noListId' isn't defined for the class".
    Nothing on this machine can compile Dart, so without this check the mistake
    is only discovered by a full CI round trip.
    """
    problems = []
    bodies = class_bodies(code)
    for name, start, end in bodies:
        body = code[start:end]
        for m in re.finditer(r"static\s+const\s+[\w<>?,\s]+\s+(\w+)\s*=", body):
            member = m.group(1)
            # Search for uses outside this class body.
            outside = code[:start] + code[end:]
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(member)}\b", outside):
                problems.append(f"{member} declared in {name} but used elsewhere")
    return problems


def duplicate_members(code: str) -> list[str]:
    """Find members declared more than once in the same class.

    This is the failure that cost a CI round trip: removing an arrow-bodied
    member by counting braces ran past its `;` into the next class and deleted
    two class headers, which Dart reports as "'dispose' is already declared in
    this scope" and "The method 'TaskDetailPage' isn't defined".

    Only DECLARATIONS are counted, which means requiring a return type before
    the name. Without that, every widget constructor call inside build() -
    `Row(`, `Text(`, `setState(` - looks like a redeclaration and the check
    drowns in false positives, which is worse than no check.
    """
    types = (r"Future<[^>]*>|Stream<[^>]*>|void|int|double|num|bool|String|"
             r"Widget|Color|IconData|DateTime|Duration|List<[^>]*>|"
             r"Map<[^>]*>|Set<[^>]*>|TextStyle|BoxDecoration|EdgeInsets")
    decl = re.compile(
        rf"^\s{{2}}(?:@override\s+)?(?:(?:static|final|late|const)\s+)*"
        rf"(?:{types})\s+(\w+)\s*\(", re.M)
    getter = re.compile(
        rf"^\s{{2}}(?:@override\s+)?(?:(?:static|final|late|const)\s+)*"
        rf"(?:{types})\s+get\s+(\w+)", re.M)

    problems = []
    for name, start, end in class_bodies(code):
        body = code[start:end]
        seen: dict[str, int] = {}
        for m in decl.finditer(body):
            seen[m.group(1)] = seen.get(m.group(1), 0) + 1
        for m in getter.finditer(body):
            seen[m.group(1)] = seen.get(m.group(1), 0) + 1
        for member, n in seen.items():
            if n > 1:
                problems.append(f"{member} declared {n}x in {name}")
    return problems


CHECKS = [
    # (name, regex, must_be_absent, why)
    ("material import", r"import\s+'package:flutter/material\.dart'", True,
     "Material import breaks this Cupertino-only app"),
    ("Colors. usage", r"(?<![A-Za-z_])Colors\.", True,
     "Colors is Material; use CupertinoColors"),
    ("RefreshIndicator", r"(?<![A-Za-z_])RefreshIndicator\(", True,
     "Material widget; use CupertinoSliverRefreshControl"),
    ("late final", r"\blate\s+final\b", True,
     "a second assignment throws LateInitializationError and blanks the screen"),
    ("ListView", r"(?<![A-Za-z_])ListView(?:\.\w+)?\(", True,
     "use CustomScrollView + SliverList"),
    ("GridView", r"(?<![A-Za-z_])GridView(?:\.\w+)?\(", True,
     "use CustomScrollView + SliverList"),
    ("const Duration(runtime)", r"const\s+Duration\([^)]*\b[a-z_]\w*\s*\)", True,
     "const with a runtime value is a compile error"),
]

# Requirements differ per app: the finance app has no calendar and no task list,
# and the command console was REMOVED on the user's instruction ("Only Telegram —
# the app is just for viewing"), so demanding it would enforce a design he
# explicitly rejected. What each app must have is what he asked for.
REQUIRED = [
    ("CupertinoSliverRefreshControl", "pull-to-refresh", set(APPS)),
    # The calendar must be an hour grid, not an agenda list. His complaint was
    # that it "only show dates not hours".
    ("kHourHeight", "hour-by-hour day grid", {"vector-calendar"}),
    ("kDayStartHour", "explicit day range", {"vector-calendar"}),
    # Tasks must read as a checklist with the priority visible.
    ("_priorityLabel", "visible priority label", {"vector-tasks"}),
    ("_toggleTask", "tappable complete checkbox", {"vector-tasks"}),
]

# A command console must NOT be present any more.
FORBIDDEN = [
    ("command(", "command console call"),
    ("_commandCard", "command console card"),
]


def main() -> int:
    fails = 0
    for app in APPS:
        path = ROOT / app / "lib" / "main.dart"
        print(f"\n=== {app} ===")
        if not path.exists():
            print("  FAIL main.dart missing")
            fails += 1
            continue
        raw = path.read_text(encoding="utf-8")
        code = strip_comments(raw)
        print(f"  lines: {len(raw.splitlines())}")

        ok, why = balanced(code)
        if ok:
            print("  ok   brackets balanced")
        else:
            print(f"  FAIL brackets: {why}")
            fails += 1

        # library; must precede every import.
        lib_at = code.find("library;")
        imp_at = code.find("import ")
        if lib_at != -1 and imp_at != -1 and imp_at < lib_at:
            print("  FAIL an import precedes 'library;'")
            fails += 1
        else:
            print("  ok   'library;' precedes imports")

        for name, pattern, absent, why in CHECKS:
            hits = [m.group(0) for m in re.finditer(pattern, code)]
            if absent and hits:
                # Count real occurrences, ignoring AppColors/CupertinoColors.
                print(f"  FAIL {name}: {len(hits)}x {hits[:3]} -- {why}")
                fails += 1
            elif absent:
                print(f"  ok   no {name}")

        for kind in ("List", "Map", "num", "String"):
            bad = guarded_casts(code, kind)
            if bad:
                print(f"  FAIL unguarded 'as {kind}': {len(bad)}x -- "
                      "throws inside build() on unexpected data; use an is-check")
                fails += 1
            else:
                print(f"  ok   no unguarded 'as {kind}'")

        misplaced = misplaced_members(code)
        if misplaced:
            for prob in misplaced:
                print(f"  FAIL out-of-scope member: {prob}")
            fails += 1
        else:
            print("  ok   no out-of-scope class members")

        dupes = duplicate_members(code)
        if dupes:
            for prob in dupes:
                print(f"  FAIL duplicate member: {prob}")
            fails += 1
        else:
            print("  ok   no duplicate members in a class")

        # .first must be guarded nearby.
        for m in re.finditer(r"\.first\b", code):
            window = code[max(0, m.start() - 120):m.start()]
            if "isNotEmpty" not in window and "length" not in window:
                print(f"  WARN .first at offset {m.start()} may be unguarded")
                break

        # api_client must be byte-identical to the shared source.
        shared = ROOT / "shared_dart" / "api_client.dart"
        local = ROOT / app / "lib" / "api_client.dart"
        if shared.read_bytes() != local.read_bytes():
            print("  FAIL api_client.dart differs from shared_dart/")
            fails += 1
        else:
            print("  ok   api_client.dart in sync")

        # The command console is the point of the product; it must be wired.
        for need, label, apps in REQUIRED:
            if app not in apps:
                continue
            if need in code:
                print(f"  ok   uses {label}")
            else:
                print(f"  FAIL missing {label}")
                fails += 1

        for needle, label in FORBIDDEN:
            if needle in code:
                print(f"  FAIL {label} should have been removed")
                fails += 1
            else:
                print(f"  ok   no {label}")

    print()
    if fails:
        print(f"STATIC VERIFICATION FAILED ({fails} issue(s))")
        return 1
    print("STATIC VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
