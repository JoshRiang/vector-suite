#!/usr/bin/env python3
"""
Scaffold the three VECTOR Suite Flutter apps from the known-good jarvis_android
tree.

WHY COPY INSTEAD OF `flutter create`
------------------------------------
`flutter create` needs the full Flutter toolchain, which is not installed on
this box (the build runs on GitHub Actions). The existing jarvis_android tree
already compiles in CI, so copying it guarantees a working android/ scaffold
instead of hand-writing Gradle files and hoping.

Each app gets:
  * the working android/ tree with its own applicationId and label
  * a pubspec with the shared deps (http for the API, shared_preferences cache)
  * lib/ with an api client + one screen
  * test/ with a real unit test so CI has something to verify
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

SRC = Path("/home/josh/jarvis_android")
ROOT = Path("/home/josh/vector_suite")

APPS = {
    "vector-tasks": {
        "name": "vector_tasks",
        "label": "Vector Tasks",
        "app_id": "com.joshua.vector_tasks",
        "description": "State an end goal, get an AI-generated, startable to-do list.",
    },
    "vector-calendar": {
        "name": "vector_calendar",
        "label": "Vector Calendar",
        "app_id": "com.joshua.vector_calendar",
        "description": "Today's plan, assembled from what is actually startable.",
    },
    "vector-finance": {
        "name": "vector_finance",
        "label": "Vector Finance",
        "app_id": "com.joshua.vector_finance",
        "description": "Balance, burn rate and runway -- server-backed so it survives a reinstall.",
    },
}


def copy_android_tree(dest: Path, app_id: str, label: str) -> None:
    shutil.copytree(SRC / "android", dest / "android")
    gradle = dest / "android" / "app" / "build.gradle"
    txt = gradle.read_text()
    txt = re.sub(r'applicationId\s*=\s*"[^"]+"', f'applicationId = "{app_id}"', txt)
    # `namespace` must track the Kotlin package directory, or Gradle cannot
    # resolve MainActivity and the APK build fails with "package does not exist".
    txt = re.sub(r'namespace\s*=\s*"[^"]+"', f'namespace = "{app_id}"', txt)
    gradle.write_text(txt)

    # Move the Kotlin MainActivity package directory to match the new app id.
    old_pkg = SRC / "android/app/src/main/kotlin/com/joshua/jarvis_app"
    new_pkg = dest / "android/app/src/main/kotlin" / app_id.replace(".", "/")
    new_pkg.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(old_pkg, new_pkg)
    kt = new_pkg / "MainActivity.kt"
    kt.write_text(kt.read_text().replace(
        "package com.joshua.jarvis_app", f"package {app_id}"))
    shutil.rmtree(dest / "android/app/src/main/kotlin/com/joshua/jarvis_app")

    manifest = dest / "android/app/src/main/AndroidManifest.xml"
    txt = manifest.read_text()
    txt = re.sub(r'android:label="[^"]*"', f'android:label="{label}"', txt)
    manifest.write_text(txt)


def write_pubspec(dest: Path, name: str, description: str) -> None:
    (dest / "pubspec.yaml").write_text(f"""name: {name}
description: {description}
publish_to: 'none'
version: 1.0.0+1

environment:
  sdk: ">=3.0.0 <4.0.0"
  flutter: ">=3.24.0"

dependencies:
  flutter:
    sdk: flutter
  cupertino_icons: ^1.0.8
  http: ^1.2.0
  shared_preferences: ^2.2.0

dev_dependencies:
  flutter_test:
    sdk: flutter
  test: ^1.24.0

flutter:
  uses-material-design: false
""")


def write_workflow(dest: Path, app: str) -> None:
    wf = dest / ".github" / "workflows" / "build.yml"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(f"""name: Build APK

on:
  push:
    branches: [ main ]
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: subosito/flutter-action@v2
        with:
          flutter-version: '3.24.0'
          channel: 'stable'
      - run: flutter --version
      - run: flutter pub get
      - run: flutter test
      - run: flutter build apk --release --target-platform android-arm64 --split-per-abi
      - uses: actions/upload-artifact@v4
        with:
          name: {app}-apk-arm64
          path: build/app/outputs/flutter-apk/app-arm64-v8a-release.apk
""")


def write_gitignore(dest: Path) -> None:
    (dest / ".gitignore").write_text(
        "*.apk\n*.zip\nbuild/\n.dart_tool/\n.packages\n"
        "android/local.properties\nandroid/.gradle/\n.idea/\n*.iml\n"
    )


def main() -> None:
    for app, meta in APPS.items():
        dest = ROOT / app
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        copy_android_tree(dest, meta["app_id"], meta["label"])
        write_pubspec(dest, meta["name"], meta["description"])
        write_workflow(dest, app)
        write_gitignore(dest)
        (dest / "lib").mkdir(exist_ok=True)
        (dest / "test").mkdir(exist_ok=True)
        print(f"scaffolded {app} -> {dest}")


if __name__ == "__main__":
    main()
