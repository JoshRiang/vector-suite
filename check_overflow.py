#!/usr/bin/env python3
"""Find right-edge overflow risks in the Flutter apps.

The user's report: "give every element a text and element wrap, if it overflow to
the right". In Flutter that is almost always one of these:

  1. A Row whose children have no Flexible/Expanded, so a long title pushes the
     row past the screen edge and the text is clipped.
  2. A Text with no maxLines/overflow inside a bounded box, which clips mid-word
     instead of ellipsising.
  3. A fixed-width child (SizedBox/Container with a width) plus an unconstrained
     Text sibling in the same Row.
  4. A nested Row inside a Row with no weight anywhere, so nothing can shrink.

This is a static check because there is no Dart compiler on this machine. It
reports file:line so each finding can be fixed deliberately.

Usage: python3 check_overflow.py [app_dir ...]
Exit 0 = no findings.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path("/home/josh/vector_suite")

# Text that is a direct child of a Row and is not wrapped in anything that can
# shrink it. `Flexible(`/`Expanded(` must appear between the Row and the Text.
ROW_START = re.compile(r"\bRow\s*\(")
SHRINKER = re.compile(r"\b(Expanded|Flexible)\s*\(")


def line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def _match_block(src: str, start: int) -> tuple[int, int]:
    """Return (open_idx, close_idx) of the parenthesised group at `start`."""
    depth = 0
    i = start
    while i < len(src):
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return start, i
        i += 1
    return start, len(src) - 1


def row_findings(src: str) -> list[tuple[int, str]]:
    """Rows holding a Text that nothing allows to shrink."""
    out: list[tuple[int, str]] = []
    for m in ROW_START.finditer(src):
        # The '(' that opens this Row's argument list.
        open_idx = src.index("(", m.start())
        _, close_idx = _match_block(src, open_idx)
        body = src[open_idx:close_idx]

        # Only direct children matter; a Text inside a nested Column is laid out
        # by that Column, not by this Row.
        depth = 0
        i = 0
        child_starts: list[int] = []
        while i < len(body):
            c = body[i]
            if c in "([":
                if depth == 0:
                    child_starts.append(i)
                depth += 1
            elif c in ")]":
                depth -= 1
            i += 1

        for cs in child_starts:
            frag = body[cs:cs + 400]
            if not re.match(r"\((Text|RichText)\b", frag):
                continue
            # Walk back to the previous direct child to see if a Flexible wraps
            # this Text.
            prefix = body[:cs]
            if SHRINKER.search(prefix.split("),")[-1] if ")," in prefix else prefix):
                continue
            # A Text with its own bounded width is fine.
            if re.search(r"width:\s*\d", frag[:200]) or "ConstrainedBox" in frag[:120]:
                continue
            out.append((line_of(src, open_idx + cs), "Text in Row with no Flexible/Expanded"))
    return out


def unclipped_text_findings(src: str) -> list[tuple[int, str]]:
    """Variable Text inside a FIXED-HEIGHT parent with no maxLines.

    A Text in a Column wraps and grows its parent, which is fine. A Text inside
    something with an explicit height cannot grow, so a long title is silently
    cut off mid-word - the "element wrap" half of the user's report. Only that
    case is reported; flagging every variable Text produced ~19 findings that
    were all correct code, which makes a checker useless as a gate.
    """
    out: list[tuple[int, str]] = []
    for m in re.finditer(r"\b(SizedBox|Container|ConstrainedBox)\s*\(", src):
        open_idx = src.index("(", m.start())
        _, close_idx = _match_block(src, open_idx)
        frag = src[open_idx:close_idx]
        # Only a bounded height matters here.
        hm = re.search(r"height:\s*(\d+)", frag)
        if not hm:
            continue
        # A fixed WIDTH too means this is a sized badge/icon box (a 28x28 day
        # circle, a 26x26 checkbox): its text is a day number or an icon, not a
        # title, so it cannot overflow. Likewise a height under 16dp is a
        # divider rule, not a text container. Both were false positives.
        if re.search(r"width:\s*\d", frag):
            continue
        if int(hm.group(1)) < 16:
            continue
        if re.search(r"(maxLines|overflow|softWrap)\s*:", frag):
            continue
        # A single-line label (a date, a count) has nothing to wrap.
        if not re.search(r"\bText\s*\(\s*[\"']?\$\{?[A-Za-z_]", frag):
            continue
        out.append((line_of(src, open_idx),
                    "variable Text in a fixed-height box with no maxLines"))
    return out


def clamp_findings(src: str) -> list[tuple[int, str]]:
    """The app must clamp the system text scale.

    Android allows up to 200% font scale. With no clamp, titles, the hour gutter
    and the priority badge grow until they run off the right edge - the user's
    "if it overflow to the right" report. This is the single highest-leverage
    overflow fix, so it is asserted rather than left to chance.
    """
    out: list[tuple[int, str]] = []
    if "withClampedTextScaling" not in src:
        out.append((1, "no MediaQuery.withClampedTextScaling: 200% system font "
                       "scale will overflow rows"))
    # A closure in a const invocation is a compile error.
    for m in re.finditer(r"const CupertinoApp\s*\(", src):
        open_idx = src.index("(", m.start())
        _, close_idx = _match_block(src, open_idx)
        if "builder" in src[open_idx:close_idx]:
            out.append((line_of(src, m.start()),
                        "const CupertinoApp with a builder closure (compile error)"))
    return out


def main() -> int:
    dirs = sys.argv[1:] or ["vector-tasks", "vector-calendar", "vector-finance"]
    total = 0
    for d in dirs:
        path = ROOT / d / "lib" / "main.dart"
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        print(f"\n=== {d} ===")
        for label, fn in (("Row overflow", row_findings),
                          ("unbounded text", unclipped_text_findings),
                          ("text scaling", clamp_findings)):
            found = fn(src)
            if not found:
                print(f"  ok   no {label}")
                continue
            print(f"  {len(found)} x {label}")
            for ln, why in found[:25]:
                print(f"     line {ln}: {why}")
            total += len(found)
    print(f"\n{total} finding(s)")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
