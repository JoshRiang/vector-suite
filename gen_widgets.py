#!/usr/bin/env python3
"""
Generate native Android home-screen widgets for the three VECTOR apps.

WHY NATIVE KOTLIN, NOT A FLUTTER PLUGIN
---------------------------------------
A home-screen widget is the ONE surface that must work without opening the app
-- that is the entire point of the user's request ("access everything more
clearly"). Flutter widget packages require the Dart engine to be running to
push data, so the widget shows stale or empty content until the app is opened,
which defeats the purpose.

These widgets are plain `AppWidgetProvider`s that fetch the API directly in a
background thread and render with RemoteViews. No new dependencies, so nothing
new to break in CI.

Each widget answers the single question that app exists to answer:
  vector-tasks     -> what is the ONE thing to start right now
  vector-calendar  -> how is today going (to start / planned / focused)
  vector-finance   -> how much runway is left

The API base URL lives in a string resource so it can be changed without
touching Kotlin (and the CI workflow can override it per build).
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path("/home/josh/vector_suite")

APPS = {
    "vector-tasks": {
        "pkg": "vector_tasks",
        "widget_class": "NextActionWidget",
        "widget_name": "Next action",
        "endpoint": "/tasks/startable",
        "layout": "widget_next_action",
        "min_width": 250,
        "min_height": 110,
        "target": "tasks",
    },
    "vector-calendar": {
        "pkg": "vector_calendar",
        "widget_class": "TodayWidget",
        "widget_name": "Today",
        "endpoint": "/today",
        "layout": "widget_today",
        "min_width": 250,
        "min_height": 110,
        "target": "calendar",
    },
    "vector-finance": {
        "pkg": "vector_finance",
        "widget_class": "RunwayWidget",
        "widget_name": "Runway",
        "endpoint": "/finance",
        "layout": "widget_runway",
        "min_width": 180,
        "min_height": 110,
        "target": "finance",
    },
}

# ---------------------------------------------------------------------------
# Kotlin provider
# ---------------------------------------------------------------------------
KT = '''package com.joshua.{pkg}

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * {widget_name} home-screen widget.
 *
 * Fetches the API on a background thread (a widget provider runs on the main
 * thread, so network work here must never be done inline) and renders with
 * RemoteViews. Falls back to a readable message instead of an empty box when
 * the server is unreachable -- a widget that silently shows nothing is
 * indistinguishable from a broken app.
 */
class {widget_class} : AppWidgetProvider() {{

    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) {{
        for (id in appWidgetIds) {{
            val views = RemoteViews(context.packageName, R.layout.{layout})
            views.setTextViewText(R.id.widget_primary, context.getString(R.string.widget_loading))
            views.setTextViewText(R.id.widget_secondary, "")
            appWidgetManager.updateAppWidget(id, views)
            thread {{ refresh(context, appWidgetManager, id) }}
        }}
    }}

    private fun refresh(context: Context, mgr: AppWidgetManager, id: Int) {{
        val views = RemoteViews(context.packageName, R.layout.{layout})
        try {{
            val body = httpGet(context, "{endpoint}")
            val (primary, secondary, badge) = render(body)
            views.setTextViewText(R.id.widget_primary, primary)
            views.setTextViewText(R.id.widget_secondary, secondary)
            views.setTextViewText(R.id.widget_badge, badge)
        }} catch (e: Exception) {{
            views.setTextViewText(R.id.widget_primary, context.getString(R.string.widget_offline))
            views.setTextViewText(R.id.widget_secondary, context.getString(R.string.widget_offline_hint))
            views.setTextViewText(R.id.widget_badge, "")
        }}

        // Tapping the widget opens the app.
        val intent = Intent(context, MainActivity::class.java)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        views.setOnClickPendingIntent(
            R.id.widget_root,
            PendingIntent.getActivity(context, 0, intent, flags)
        )
        mgr.updateAppWidget(id, views)
    }}

    /** {widget_name} content. Returns (primary, secondary, badge). */
    private fun render(body: String): Triple<String, String, String> {{
{render_body}
    }}

    /** Group digits so a 7-figure amount stays readable in a narrow widget. */
    private fun fmt(v: Double): String {{
        val s = String.format("%,.0f", v)
        return s
    }}

    private fun httpGet(context: Context, path: String): String {{
        val base = context.getString(R.string.vector_api_base).trimEnd('/')
        val userId = context.getString(R.string.vector_user_id)
        val conn = (URL(base + path).openConnection() as HttpURLConnection).apply {{
            requestMethod = "GET"
            connectTimeout = 8000
            readTimeout = 8000
            setRequestProperty("X-User-Id", userId)
            setRequestProperty("Accept", "application/json")
        }}
        try {{
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            return stream?.bufferedReader()?.use {{ it.readText() }} ?: ""
        }} finally {{
            conn.disconnect()
        }}
    }}
}}
'''

RENDER = {
    "vector-tasks": '''
        // The whole product premise: surface exactly ONE startable action.
        // A widget that listed the backlog would recreate the paralysis.
        val arr = JSONArray(body)
        if (arr.length() == 0) {
            return Triple(
                "Nothing to start",
                "Open the app to set a goal",
                ""
            )
        }
        val top = arr.getJSONObject(0)
        val title = top.optString("title", "Untitled task")
        val minutes = top.optInt("minutes", 30)
        val more = arr.length() - 1
        val secondary = if (more > 0)
            "$minutes min  \\u00b7  $more more unlocked"
        else
            "$minutes min"
        return Triple(title, secondary, "START HERE")''',
    "vector-calendar": '''
        val o = JSONObject(body)
        val startable = o.optJSONArray("startable") ?: JSONArray()
        val done = o.optJSONArray("done_today") ?: JSONArray()
        val focus = o.optInt("focus_minutes", 0)
        var planned = 0
        for (i in 0 until startable.length()) {
            planned += startable.getJSONObject(i).optInt("minutes", 0)
        }
        val primary = if (startable.length() == 0)
            "Nothing to start"
        else
            startable.getJSONObject(0).optString("title", "Untitled")
        val secondary = startable.length().toString() + " to start  \\u00b7  " +
            planned.toString() + " min  \\u00b7  " + done.length().toString() + " done"
        val badge = if (focus > 0) focus.toString() + "M FOCUSED" else "TODAY"
        return Triple(primary, secondary, badge)''',
    "vector-finance": '''
        val o = JSONObject(body)
        val runway = if (o.isNull("runway_days")) null else o.optInt("runway_days")
        val free = if (o.isNull("free_today")) null else o.optDouble("free_today", 0.0)
        val currency = o.optString("currency", "IDR")
        val primary = if (runway == null) "-- days" else runway.toString() + " days"
        val secondary = if (free == null)
            "No spending logged yet"
        else
            fmt(free) + " " + currency + " free today"
        return Triple(primary, secondary, "RUNWAY")''',
}

# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------
LAYOUT = '''<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:tools="http://schemas.android.com/tools"
    android:id="@+id/widget_root"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:background="@drawable/widget_bg"
    android:padding="14dp">

    <TextView
        android:id="@+id/widget_badge"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:textSize="9sp"
        android:letterSpacing="0.12"
        android:textStyle="bold"
        android:textColor="#6366F1"
        android:maxLines="1"
        tools:ignore="SmallSp" />

    <TextView
        android:id="@+id/widget_primary"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_marginTop="3dp"
        android:textSize="15sp"
        android:textStyle="bold"
        android:textColor="#1C1C1E"
        android:maxLines="3"
        android:ellipsize="end" />

    <TextView
        android:id="@+id/widget_secondary"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_marginTop="4dp"
        android:textSize="11sp"
        android:textColor="#6B7280"
        android:maxLines="2"
        android:ellipsize="end" />
</LinearLayout>
'''

# Widget background: light glass card, rounded, subtle border.
WIDGET_BG = '''<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="rectangle">
    <solid android:color="#F7F8FC" />
    <corners android:radius="20dp" />
    <stroke android:width="1dp" android:color="#E5E7F0" />
</shape>
'''

# ---------------------------------------------------------------------------
# Widget provider info
# ---------------------------------------------------------------------------
def widget_info(meta: dict) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<appwidget-provider xmlns:android="http://schemas.android.com/apk/res/android"
    android:minWidth="{meta['min_width']}dp"
    android:minHeight="{meta['min_height']}dp"
    android:targetCellWidth="3"
    android:targetCellHeight="2"
    android:updatePeriodMillis="1800000"
    android:initialLayout="@layout/{meta['layout']}"
    android:resizeMode="horizontal|vertical"
    android:widgetCategory="home_screen"
    android:description="@string/widget_description" />
'''


def main() -> None:
    for app, meta in APPS.items():
        d = ROOT / app
        pkg = meta["pkg"]
        res = d / "android/app/src/main/res"
        kt_dir = d / f"android/app/src/main/kotlin/com/joshua/{pkg}"

        # 1. Kotlin provider
        (kt_dir / f"{meta['widget_class']}.kt").write_text(
            KT.format(
                pkg=pkg,
                widget_class=meta["widget_class"],
                widget_name=meta["widget_name"],
                layout=meta["layout"],
                endpoint=meta["endpoint"],
                render_body=RENDER[app],
            )
        )

        # 2. Layout + background + provider info
        (res / "layout").mkdir(exist_ok=True)
        (res / "layout" / f"{meta['layout']}.xml").write_text(LAYOUT)
        (res / "drawable").mkdir(exist_ok=True)
        (res / "drawable" / "widget_bg.xml").write_text(WIDGET_BG)
        (res / "xml").mkdir(exist_ok=True)
        (res / "xml" / f"{meta['layout']}_info.xml").write_text(widget_info(meta))

        print(f"generated widget for {app}: {meta['widget_class']}")


if __name__ == "__main__":
    main()
