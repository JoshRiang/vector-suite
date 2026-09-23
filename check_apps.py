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

# Symbols that only exist in package:flutter/material.dart. These apps import
# ONLY package:flutter/cupertino.dart, so using any of these is a hard compile
# error ("method not found").
#
# WHY THIS LIST IS LONG: a short list gives false confidence. An earlier
# version omitted RefreshIndicator, the apps passed the local check, and two
# CI builds failed on it -- a check that misses real bugs is worse than no
# check, because it is trusted.
#
# Matched with a negative lookbehind for "Cupertino" so CupertinoColors,
# CupertinoPageScaffold, CupertinoSliverRefreshControl etc. do not trip it.
MATERIAL_ONLY = [
    # scaffolding / app shell
    r"(?<!Cupertino)\bMaterialApp\b",
    r"(?<!Cupertino)\bScaffold\(",
    r"(?<!Cupertino)\bAppBar\(",
    r"(?<!Cupertino)\bMaterial\(",
    r"(?<!Cupertino)\bTheme\(",
    r"(?<!Cupertino)\bThemeData\b",
    # progress + refresh
    r"\bRefreshIndicator\b",
    r"\bLinearProgressIndicator\b",
    r"\bCircularProgressIndicator\b",
    # text input + buttons
    r"(?<!Cupertino)\bTextField\(",
    r"(?<!Cupertino)\bTextFormField\b",
    r"\bElevatedButton\b",
    r"\bTextButton\b",
    r"\bOutlinedButton\b",
    r"\bIconButton\b",
    r"\bFloatingActionButton\b",
    r"\bDropdownButton\b",
    r"\bCheckbox\b",
    r"\bSwitch\(",
    r"\bRadio\(",
    r"\bSlider\(",
    # lists, tiles, dividers
    r"(?<!Cupertino)\bListTile\b",
    r"(?<!Cupertino)\bDivider\(",
    r"(?<!Cupertino)\bListView\(",
    r"(?<!Cupertino)\bGridView\(",
    r"\bExpansionTile\b",
    r"\bStepper\b",
    # sheets, dialogs, snackbars
    r"\bshowDialog\b",
    r"\bAlertDialog\b",
    r"\bBottomSheet\b",
    r"\bSnackBar\b",
    r"\bScaffoldMessenger\b",
    r"\bDrawer\(",
    r"\bTabBar\(",
    r"\bBottomNavigationBar\b",
    # theming helpers
    r"(?<!Cupertino)\bColors\.",
    r"(?<!Cupertino)\bIcons\.",
    r"(?<!Cupertino)\bTextTheme\b",
    r"(?<!Cupertino)\bColorScheme\b",
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

        # 2b. The strongest possible form of the check above: these apps must
        #     never import Material at all. If this holds, every Material widget
        #     is a compile error, so the list above is belt-and-braces.
        if re.search(r"import\s+'package:flutter/material\.dart'", src):
            errs.append(f"{rel}: imports package:flutter/material.dart "
                        f"(these apps are Cupertino-only)")

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
    else:
        wtext = wf.read_text()
        if "flutter test" not in wtext:
            errs.append("CI workflow does not run flutter test")
        if "API_BASE" not in wtext:
            errs.append("CI workflow does not inject API_BASE")
        if "API_KEY" not in wtext:
            errs.append("CI workflow does not inject API_KEY")

    # 8. Home-screen widget wiring.
    #    A provider that is generated but not declared in the manifest compiles
    #    and ships, yet NEVER appears in the launcher's widget picker -- a
    #    silent failure with no build error to catch it. So every one of these
    #    is checked explicitly.
    manifest = (d / "android/app/src/main/AndroidManifest.xml").read_text()
    res = d / "android/app/src/main/res"

    providers = list((d / "android/app/src/main/kotlin").rglob("*Widget.kt"))
    if not providers:
        errs.append("no AppWidgetProvider class found")
    for prov in providers:
        cls = prov.stem
        src_k = prov.read_text()

        if f'android:name=".{cls}"' not in manifest:
            errs.append(f"widget {cls} not declared as <receiver> in manifest")
        if "android.appwidget.action.APPWIDGET_UPDATE" not in manifest:
            errs.append("manifest missing APPWIDGET_UPDATE intent-filter")
        if "android.appwidget.provider" not in manifest:
            errs.append("manifest missing android.appwidget.provider metadata")

        # Provider class must extend AppWidgetProvider and read the shared
        # endpoint resource, or the widget would point at a different server
        # than the app.
        if "AppWidgetProvider" not in src_k:
            errs.append(f"{cls}: does not extend AppWidgetProvider")
        if "R.string.vector_api_base" not in src_k:
            errs.append(f"{cls}: does not read vector_api_base")
        if "thread {" not in src_k and "Thread(" not in src_k:
            errs.append(f"{cls}: does network work without a background thread")

        # Every referenced layout/id/resource must exist, or AAPT fails.
        for m in re.finditer(r"R\.layout\.(\w+)", src_k):
            if not (res / "layout" / f"{m.group(1)}.xml").exists():
                errs.append(f"{cls}: R.layout.{m.group(1)} does not exist")
        for m in re.finditer(r"R\.string\.(\w+)", src_k):
            strings = res / "values/strings.xml"
            if strings.exists() and f'name="{m.group(1)}"' not in strings.read_text():
                errs.append(f"{cls}: R.string.{m.group(1)} not in strings.xml")

        # Every R.id must exist in one of this app's layouts. This is the same
        # class of failure as a missing string, but it slipped through once: a
        # shared helper referencing row ids that only one app's layout defined
        # compiled fine in that app and broke the other two. Checking it here
        # catches it before CI does.
        layout_ids: set[str] = set()
        for lay in (res / "layout").glob("*.xml"):
            layout_ids |= set(re.findall(
                r'android:id="@\+id/(\w+)"', lay.read_text()))
        for m in re.finditer(r"R\.id\.(\w+)", src_k):
            if m.group(1) not in layout_ids:
                errs.append(f"{cls}: R.id.{m.group(1)} not in any layout")

        # The provider-info XML the manifest points at must exist.
        for m in re.finditer(r'android:resource="@xml/(\w+)"', manifest):
            if not (res / "xml" / f"{m.group(1)}.xml").exists():
                errs.append(f"@xml/{m.group(1)} referenced but missing")

        # Layouts must declare every namespace they use.
        for lay in (res / "layout").glob("*.xml"):
            ls = lay.read_text()
            for prefix in ("tools", "app"):
                if f"{prefix}:" in ls and f"xmlns:{prefix}=" not in ls:
                    errs.append(f"{lay.name}: uses {prefix}: without xmlns:{prefix}")

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
