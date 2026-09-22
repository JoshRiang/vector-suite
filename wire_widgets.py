#!/usr/bin/env python3
"""
Wire the generated widgets into each app's AndroidManifest and strings.

WHY THIS IS A SEPARATE STEP FROM GENERATION
-------------------------------------------
Generating the Kotlin/layout files does nothing on its own: an AppWidgetProvider
is only discoverable by the launcher if it is declared as a <receiver> in the
manifest with the APPWIDGET_UPDATE intent-filter and its metadata. Forgetting
that produces a widget that compiles, ships, and never appears in the widget
picker -- a silent failure. This script makes the declaration mandatory and
idempotent.

It also injects the API base URL and user id as string resources so the CI
build can override them without editing Kotlin.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("/home/josh/vector_suite")

APPS = {
    "vector-tasks": dict(pkg="vector_tasks", cls="NextActionWidget",
                         info="widget_next_action_info", label="Next action"),
    "vector-calendar": dict(pkg="vector_calendar", cls="TodayWidget",
                            info="widget_today_info", label="Today"),
    "vector-finance": dict(pkg="vector_finance", cls="RunwayWidget",
                           info="widget_runway_info", label="Runway"),
}

DEFAULT_API = "http://100.89.180.23:8790"
USER_ID = "josh"


def add_strings(app_dir: Path, label: str) -> None:
    """Add widget + config strings. Creates values/strings.xml if absent."""
    values = app_dir / "android/app/src/main/res/values"
    values.mkdir(parents=True, exist_ok=True)
    strings = values / "strings.xml"

    entries = {
        "widget_label": label,
        "widget_loading": "Loading&#8230;",
        "widget_offline": "Server unreachable",
        "widget_offline_hint": "Check your connection",
        "widget_description": f"{label} from VECTOR",
        "vector_api_base": DEFAULT_API,
        "vector_user_id": USER_ID,
    }

    if strings.exists():
        txt = strings.read_text()
        # Drop any prior copies of our keys so re-running is idempotent.
        for k in entries:
            txt = re.sub(rf'\s*<string name="{k}">.*?</string>', "", txt)
        # Also drop the placeholder app_name if present (label comes from manifest).
        body = "\n".join(
            f'    <string name="{k}">{v}</string>' for k, v in entries.items()
        )
        txt = txt.replace("</resources>", body + "\n</resources>")
        strings.write_text(txt)
    else:
        body = "\n".join(
            f'    <string name="{k}">{v}</string>' for k, v in entries.items()
        )
        strings.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<resources>\n" + body + "\n</resources>\n"
        )
    print(f"  strings.xml updated ({len(entries)} keys)")


def add_receiver(app_dir: Path, meta: dict) -> None:
    """Declare the AppWidgetProvider receiver. Idempotent."""
    manifest = app_dir / "android/app/src/main/AndroidManifest.xml"
    txt = manifest.read_text()

    receiver = f'''
        <!-- Home-screen widget. Without this declaration the provider class is
             never discovered by the launcher and the widget silently never
             appears in the picker. -->
        <receiver
            android:name=".{meta['cls']}"
            android:label="@string/widget_label"
            android:exported="false">
            <intent-filter>
                <action android:name="android.appwidget.action.APPWIDGET_UPDATE" />
            </intent-filter>
            <meta-data
                android:name="android.appwidget.provider"
                android:resource="@xml/{meta['info']}" />
        </receiver>
'''

    if f'android:name=".{meta["cls"]}"' in txt:
        # Replace the existing block so edits to it take effect.
        txt = re.sub(
            rf'\n\s*<!-- Home-screen widget.*?</receiver>\n',
            receiver, txt, flags=re.DOTALL)
        print(f"  receiver {meta['cls']} refreshed")
    else:
        txt = txt.replace("</application>", receiver + "    </application>")
        print(f"  receiver {meta['cls']} added")

    manifest.write_text(txt)


def main() -> None:
    for app, meta in APPS.items():
        d = ROOT / app
        print(f"{app}:")
        add_strings(d, meta["label"])
        add_receiver(d, meta)


if __name__ == "__main__":
    main()
