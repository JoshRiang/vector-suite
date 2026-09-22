#!/usr/bin/env python3
"""
Structural sanity checks for the VECTOR Suite Flutter apps.

WHY: there is no Flutter toolchain on this box (CI builds the APK), so a real
compile is not available locally. These checks catch the failure classes that
actually waste a CI cycle -- unbalanced braces, missing imports for a symbol
that is used, a widget class the app never defines, and package-name drift
between pubspec and the test imports.

They are NOT a substitute for `flutter build`. They are the cheap gate before
spending a CI run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path("/home/josh/vector_suite")
APPS = {
    "vector-tasks": "vector_tasks",
    "vector-calendar": "vector_calendar",
    "vector-finance": "vector_finance",
}

# Symbols that only exist in package:flutter/material.dart. These apps are
# Cupertino-only (uses-material-design: false), so using them is a real bug
# that standalone analysis often misses.
# NOTE: matched with a negative lookbehind for "Cupertino" -- `CupertinoColors`
# and `CupertinoPageScaffold` are legitimate and must not trip the check.
MATERIAL_ONLY = [
    r"(?<!Cupertino)\bColors\.",
    r"(?<!Cupertino)\bScaffold\(",
    r"\bMaterialApp\b",
    r"(?<!Cupertino)\bListTile\b",
    r"(?<!Cupertino)\bDivider\(",
    r"\bLinearProgressIndicator\b",
    r"\bCircularProgressIndicator\b",
    r"\bTextField\(",
    r"\bElevatedButton\b",
]

# Packages that are framework-provided, not the app's own package name.
FRAMEWORK_PKGS = {"flutter", "flutter_test", "flutter_localizations"}


def strip_comments(src: str) -> str:
    """Remove Dart comments while respecting string literals.

    A naive regex strips `//` inside a string literal (e.g. the URL
    'http://...'), which truncates the rest of the line and produces bogus
    imbalance reports. This walks the source instead.
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if quote:
            out.append(ch)
            if ch == "\\":            # skip the escaped character
                if nxt:
                    out.append(nxt)
                    i += 2
                    continue
            elif src.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
            i += 1
            continue

        # Not inside a string: check for comment starts and quote starts.
        if ch == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if src.startswith("'''", i):
            quote, i = "'''", i + 3
            continue
        if src.startswith('"""', i):
            quote, i = '"""', i + 3
            continue
        if ch in ("'", '"'):
            quote, i = ch, i + 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def balanced(src: str) -> list[str]:
    """Report unbalanced delimiters in already-comment-stripped source."""
    errs = []
    for open_c, close_c, label in (("{", "}", "braces"), ("(", ")", "parens"),
                                   ("[", "]", "brackets")):
        if src.count(open_c) != src.count(close_c):
            errs.append(f"unbalanced {label} "
                        f"({src.count(open_c)} vs {src.count(close_c)})")
    return errs


def check(app: str, pkg: str) -> list[str]:
    errs: list[str] = []
    d = ROOT / app

    pubspec = (d / "pubspec.yaml").read_text()
    if f"name: {pkg}" not in pubspec:
        errs.append(f"pubspec name != {pkg}")
    if "uses-material-design: false" not in pubspec:
        errs.append("uses-material-design should be false for Cupertino-only")
    for dep in ("http:", "shared_preferences:"):
        if dep not in pubspec:
            errs.append(f"missing dependency {dep}")

    for f in sorted((d / "lib").glob("*.dart")) + sorted((d / "test").glob("*.dart")):
        raw = f.read_text()
        src = strip_comments(raw)
        rel = f.relative_to(d)

        # 1. Delimiter balance, checked on comment/string-aware source so that
        #    a '//' inside a URL or a brace inside a comment cannot mislead it.
        for e in balanced(src):
            errs.append(f"{rel}: {e}")

        # 2. A Material-only symbol in a Cupertino-only app.
        for pat in MATERIAL_ONLY:
            m = re.search(pat, src)
            if m:
                errs.append(f"{rel}: uses Material-only symbol '{m.group(0)}'")

        # 3. Imports used by the file must be declared.
        if "http." in src and "package:http/http.dart" not in src:
            errs.append(f"{rel}: uses http. but does not import package:http")
        if "SharedPreferences" in src and "shared_preferences" not in src:
            errs.append(f"{rel}: uses SharedPreferences without importing it")

        # 4. Relative import of the sibling client must resolve.
        for m in re.finditer(r"import '([^:]+\.dart)';", src):
            target = (f.parent / m.group(1)).resolve()
            if not target.exists():
                errs.append(f"{rel}: broken import -> {m.group(1)}")

        # 5. Test files must import their own package by the right name
        #    (framework packages are not the app's own package).
        if f.parent.name == "test":
            for m in re.finditer(r"package:([a-z_]+)/", src):
                if m.group(1) not in FRAMEWORK_PKGS and m.group(1) != pkg:
                    errs.append(
                        f"{rel}: imports package:{m.group(1)} but app is {pkg}")

    # 6. Android identity must be internally consistent.
    gradle = (d / "android/app/build.gradle").read_text()
    app_id = f"com.joshua.{pkg}"
    if f'applicationId = "{app_id}"' not in gradle:
        errs.append(f"build.gradle applicationId != {app_id}")
    if f'namespace = "{app_id}"' not in gradle:
        errs.append(f"build.gradle namespace != {app_id}")
    kt = d / f"android/app/src/main/kotlin/com/joshua/{pkg}/MainActivity.kt"
    if not kt.exists():
        errs.append(f"MainActivity.kt missing at {kt.relative_to(d)}")
    elif f"package {app_id}" not in kt.read_text():
        errs.append("MainActivity.kt package declaration mismatch")

    # 7. The APK workflow must exist and reference flutter test.
    wf = d / ".github/workflows/build.yml"
    if not wf.exists():
        errs.append("missing .github/workflows/build.yml")
    elif "flutter test" not in wf.read_text():
        errs.append("CI workflow does not run flutter test")

    return errs


def main() -> int:
    total = 0
    for app, pkg in APPS.items():
        errs = check(app, pkg)
        if errs:
            total += len(errs)
            print(f"FAIL {app}")
            for e in errs:
                print(f"   - {e}")
        else:
            print(f"OK   {app}")
    print()
    print("ALL STRUCTURAL CHECKS PASSED" if total == 0
          else f"{total} problem(s) found")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
