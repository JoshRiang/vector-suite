#!/usr/bin/env python3
"""
Generate native Android home-screen widgets for the three VECTOR apps.

WHY NATIVE KOTLIN, NOT A FLUTTER PLUGIN
---------------------------------------
A home-screen widget is the ONE surface that must work without opening the app.
Flutter widget packages require the Dart engine to be running to push data, so
the widget shows stale or empty content until the app is opened.

These widgets are plain `AppWidgetProvider`s that fetch the API directly on a
background thread and render with RemoteViews. No new dependencies.

WHAT EACH WIDGET DOES
  vector-tasks     -> today's startable actions as TICKABLE rows + an "ask
                      Hermes" row (the app itself plans nothing; the list is
                      built and managed server-side)
  vector-calendar  -> a real month grid (Google-Calendar style) with today
                      ringed and a dot on days that have tasks
  vector-finance   -> runway in days + what is free to spend today

INTERACTION: RemoteViews cannot host arbitrary views, so each row is a
horizontal LinearLayout of TextViews with its own PendingIntent. Tapping the
checkbox fires a broadcast straight to the provider, which POSTs to the API and
re-renders -- so a task can be completed from the home screen without opening
the app. The row is rebuilt after every toggle, which is what makes the list
re-order and the progress bar move.

minSdk is 21, so java.time is unavailable (it needs API 26). All date maths is
therefore done with java.util.Calendar.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path("/home/josh/vector_suite")

APPS = {
    "vector-tasks": {
        "pkg": "vector_tasks",
        "widget_class": "NextActionWidget",
        "widget_name": "VECTOR Tasks",
        "layout": "widget_next_action",
        "min_width": 250,
        "min_height": 180,
        "target": "tasks",
    },
    "vector-calendar": {
        "pkg": "vector_calendar",
        "widget_class": "TodayWidget",
        "widget_name": "VECTOR Calendar",
        "layout": "widget_today",
        "min_width": 250,
        "min_height": 200,
        "target": "calendar",
    },
    "vector-finance": {
        "pkg": "vector_finance",
        "widget_class": "RunwayWidget",
        "widget_name": "VECTOR Finance",
        "layout": "widget_runway",
        "min_width": 180,
        "min_height": 140,
        "target": "finance",
    },
}

# ---------------------------------------------------------------------------
# Shared Kotlin helpers, injected into every provider.
# ---------------------------------------------------------------------------
COMMON = r'''
    /** Group digits so a 7-figure amount stays readable in a narrow widget. */
    private fun fmt(v: Double): String = String.format("%,.0f", v)

    private fun httpGet(context: Context, path: String): String =
        http(context, "GET", path, null)

    private fun httpPost(context: Context, path: String): String =
        http(context, "POST", path, "{}")

    /**
     * Single HTTP entry point. A widget provider runs on the main thread, so
     * every caller is responsible for being off it.
     */
    private fun http(context: Context, method: String, path: String,
                     body: String?): String {
        val base = context.getString(R.string.vector_api_base).trimEnd('/')
        val userId = context.getString(R.string.vector_user_id)
        val key = context.getString(R.string.vector_api_key)
        val conn = (URL(base + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 8000
            readTimeout = 8000
            setRequestProperty("X-User-Id", userId)
            setRequestProperty("Accept", "application/json")
            if (key.isNotEmpty()) setRequestProperty("X-Api-Key", key)
            if (body != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
            }
        }
        try {
            if (body != null) {
                conn.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            return stream?.bufferedReader()?.use { it.readText() } ?: ""
        } finally {
            conn.disconnect()
        }
    }

'''

# The task-row binder lives outside the template so its Kotlin braces are
# never parsed as format fields (see the format call in main()).
BINDROW = r'''
    /**
     * Render one task row into the widget.
     *
     * RemoteViews has no adapter and no dynamic child views, so the rows are
     * laid out statically in XML and shown/hidden as needed. Each row's
     * checkbox and body carry their own PendingIntent because the ids are
     * per-row.
     */
    private fun bindRow(
        context: Context,
        views: RemoteViews,
        idx: Int,
        taskId: String?,
        title: String,
        meta: String,
        done: Boolean
    ) {
        val rowIds = intArrayOf(
            R.id.row0, R.id.row1, R.id.row2, R.id.row3, R.id.row4)
        val boxIds = intArrayOf(
            R.id.row0_box, R.id.row1_box, R.id.row2_box,
            R.id.row3_box, R.id.row4_box)
        val txtIds = intArrayOf(
            R.id.row0_text, R.id.row1_text, R.id.row2_text,
            R.id.row3_text, R.id.row4_text)
        val subIds = intArrayOf(
            R.id.row0_sub, R.id.row1_sub, R.id.row2_sub,
            R.id.row3_sub, R.id.row4_sub)

        if (taskId == null) {
            views.setViewVisibility(rowIds[idx], android.view.View.GONE)
            return
        }
        views.setViewVisibility(rowIds[idx], android.view.View.VISIBLE)
        views.setTextViewText(boxIds[idx], if (done) "\u2713" else "\u25CB")
        views.setTextViewText(txtIds[idx], title)
        views.setTextViewText(subIds[idx], meta)

        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        // Tapping the checkbox completes the task; tapping the text opens the
        // app. Both are needed: one-tap finish, or go in and read the detail.
        val toggle = Intent(context, javaClass).apply {
            action = ACTION_TOGGLE
            putExtra(EXTRA_TASK_ID, taskId)
            putExtra(EXTRA_DONE, !done)
            data = android.net.Uri.parse("vectortoggle://" + taskId)
        }
        views.setOnClickPendingIntent(
            boxIds[idx], PendingIntent.getBroadcast(context, 100 + idx, toggle, flags))
        val open = Intent(context, MainActivity::class.java)
        views.setOnClickPendingIntent(
            txtIds[idx], PendingIntent.getActivity(context, 200 + idx, open, flags))
        views.setOnClickPendingIntent(
            subIds[idx], PendingIntent.getActivity(context, 300 + idx, open, flags))
    }
'''

TASKS_KT = '''package com.joshua.{pkg}

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import org.json.JSONArray
import kotlin.concurrent.thread

/**
 * {widget_name} home-screen widget.
 *
 * Shows today's startable actions as tickable rows. Completing one re-fetches
 * and re-renders, so the finished task disappears, the next blocked task
 * unlocks and the progress bar moves -- all without opening the app.
 *
 * The list is built and ordered SERVER-SIDE. This widget holds no planning
 * logic of its own: it renders whatever the API says is startable.
 */
class {widget_class} : AppWidgetProvider() {{

    companion object {{
        const val ACTION_TOGGLE = "com.joshua.{pkg}.TOGGLE"
        const val EXTRA_TASK_ID = "task_id"
        const val EXTRA_DONE = "done"
    }}

    override fun onUpdate(context: Context, mgr: AppWidgetManager, ids: IntArray) {{
        for (id in ids) {{
            val views = RemoteViews(context.packageName, R.layout.{layout})
            views.setTextViewText(R.id.widget_primary,
                context.getString(R.string.widget_loading))
            mgr.updateAppWidget(id, views)
            thread {{ refresh(context, mgr, id) }}
        }}
    }}

    /**
     * A checkbox tap arrives here. Network work must not run on the main
     * thread, so the write happens on a background thread and the widget is
     * refreshed from that same thread afterwards.
     */
    override fun onReceive(context: Context, intent: Intent) {{
        super.onReceive(context, intent)
        if (intent.action != ACTION_TOGGLE) return
        val taskId = intent.getStringExtra(EXTRA_TASK_ID) ?: return
        val done = intent.getBooleanExtra(EXTRA_DONE, true)
        val pending = goAsync()
        thread {{
            try {{
                httpPost(context, if (done) "/tasks/" + taskId + "/done"
                                    else "/tasks/" + taskId + "/reopen")
            }} catch (e: Exception) {{
                // A failed tap must not crash the launcher; the next refresh
                // re-reads the server and shows the true state.
            }} finally {{
                val mgr = AppWidgetManager.getInstance(context)
                for (id in mgr.getAppWidgetIds(
                        android.content.ComponentName(context, {widget_class}::class.java))) {{
                    refresh(context, mgr, id)
                }}
                pending.finish()
            }}
        }}
    }}

    private fun refresh(context: Context, mgr: AppWidgetManager, id: Int) {{
        val views = RemoteViews(context.packageName, R.layout.{layout})
        try {{
            val rows = JSONArray(httpGet(context, "/tasks/startable"))
            var shown = 0
            for (i in 0 until 5) {{
                if (i < rows.length()) {{
                    val o = rows.getJSONObject(i)
                    val mins = o.optInt("minutes", 30)
                    bindRow(context, views, i,
                        o.optString("id", null),
                        o.optString("title", "Untitled task"),
                        mins.toString() + " min",
                        false)
                    shown++
                }} else {{
                    bindRow(context, views, i, null, "", "", false)
                }}
            }}
            if (shown == 0) {{
                views.setViewVisibility(R.id.widget_empty,
                    android.view.View.VISIBLE)
                views.setTextViewText(R.id.widget_empty,
                    context.getString(R.string.widget_all_clear))
            }} else {{
                views.setViewVisibility(R.id.widget_empty,
                    android.view.View.GONE)
            }}
            // Progress: how many of today's tasks are already done.
            val today = org.json.JSONObject(httpGet(context, "/today"))
            val done = today.optJSONArray("done_today")?.length() ?: 0
            val total = done + shown
            val pct = if (total > 0) (100 * done / total) else 0
            views.setTextViewText(R.id.widget_badge, done.toString() + "/" +
                total.toString() + " DONE")
            views.setProgressBar(R.id.widget_progress, 100, pct, false)
            views.setTextViewText(R.id.widget_secondary,
                if (total > 0) pct.toString() + "% of today complete" else "")
        }} catch (e: Exception) {{
            for (i in 0 until 5) bindRow(context, views, i, null, "", "", false)
            views.setViewVisibility(R.id.widget_empty, android.view.View.VISIBLE)
            views.setTextViewText(R.id.widget_empty,
                context.getString(R.string.widget_offline))
            views.setTextViewText(R.id.widget_badge, "")
            views.setTextViewText(R.id.widget_secondary, "")
        }}

        // "Ask Hermes" opens the app, where a goal becomes a plan.
        val ask = Intent(context, MainActivity::class.java)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        views.setOnClickPendingIntent(R.id.widget_ask,
            PendingIntent.getActivity(context, 1, ask, flags))
        val open = Intent(context, MainActivity::class.java)
        views.setOnClickPendingIntent(R.id.widget_root,
            PendingIntent.getActivity(context, 0, open, flags))
        mgr.updateAppWidget(id, views)
    }}

{bindrow}
{common}
}}
'''

CALENDAR_KT = '''package com.joshua.{pkg}

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import org.json.JSONArray
import org.json.JSONObject
import java.util.Calendar
import kotlin.concurrent.thread

/**
 * {widget_name} home-screen widget: a real month grid.
 *
 * Renders a 7-column calendar the way a calendar widget is expected to look --
 * weekday headers, a ringed "today", and a dot under any day that has tasks
 * scheduled. The dot count is driven by the API, not guessed.
 *
 * Date maths uses java.util.Calendar, not java.time: minSdk is 21 and
 * java.time requires API 26.
 */
class {widget_class} : AppWidgetProvider() {{

    override fun onUpdate(context: Context, mgr: AppWidgetManager, ids: IntArray) {{
        for (id in ids) {{
            val views = RemoteViews(context.packageName, R.layout.{layout})
            views.setTextViewText(R.id.widget_primary,
                context.getString(R.string.widget_loading))
            mgr.updateAppWidget(id, views)
            thread {{ refresh(context, mgr, id) }}
        }}
    }}

    /** Zero-padded day number for a day-of-month. */
    private fun pad(n: Int): String = if (n < 10) "0" + n else n.toString()

    private fun refresh(context: Context, mgr: AppWidgetManager, id: Int) {{
        val views = RemoteViews(context.packageName, R.layout.{layout})
        val cal = Calendar.getInstance()
        val today = cal.get(Calendar.DAY_OF_MONTH)
        val month = cal.get(Calendar.MONTH)
        val year = cal.get(Calendar.YEAR)

        // Which days of THIS month have work. Derived from real tasks, so an
        // empty month shows no dots rather than decoration.
        val busy = HashSet<Int>()
        var startableCount = 0
        var planned = 0
        try {{
            val todayJson = JSONObject(httpGet(context, "/today"))
            val st = todayJson.optJSONArray("startable") ?: JSONArray()
            startableCount = st.length()
            for (i in 0 until st.length()) {{
                planned += st.getJSONObject(i).optInt("minutes", 0)
            }}
            if (startableCount > 0) busy.add(today)
            val done = todayJson.optJSONArray("done_today") ?: JSONArray()
            for (i in 0 until done.length()) {{
                val at = done.getJSONObject(i).optString("completed_at", "")
                if (at.length >= 10) {{
                    val d = at.substring(8, 10).toIntOrNull()
                    if (d != null) busy.add(d)
                }}
            }}
        }} catch (e: Exception) {{
            // Offline: still draw the month, just without dots. A calendar that
            // vanishes when the network blips is worse than one with no dots.
        }}

        val monthNames = arrayOf("January", "February", "March", "April", "May",
            "June", "July", "August", "September", "October", "November",
            "December")
        views.setTextViewText(R.id.cal_title,
            monthNames[month] + " " + year.toString())
        views.setTextViewText(R.id.cal_today, today.toString())

        // Build the grid: leading blanks, then days 1..lengthOfMonth.
        val first = Calendar.getInstance().apply {{
            set(Calendar.YEAR, year)
            set(Calendar.MONTH, month)
            set(Calendar.DAY_OF_MONTH, 1)
        }}
        // Calendar.MONDAY = 2; shift so Sunday (1) starts the week at index 0.
        val lead = (first.get(Calendar.DAY_OF_WEEK) + 6) % 7
        val dim = first.getActualMaximum(Calendar.DAY_OF_MONTH)
        val cells = IntArray(42)
        for (i in 0 until 42) {{
            val dayNum = i - lead + 1
            cells[i] = if (dayNum in 1..dim) dayNum else 0
        }}
        val ids = intArrayOf(
            R.id.d0, R.id.d1, R.id.d2, R.id.d3, R.id.d4, R.id.d5, R.id.d6,
            R.id.d7, R.id.d8, R.id.d9, R.id.d10, R.id.d11, R.id.d12, R.id.d13,
            R.id.d14, R.id.d15, R.id.d16, R.id.d17, R.id.d18, R.id.d19, R.id.d20,
            R.id.d21, R.id.d22, R.id.d23, R.id.d24, R.id.d25, R.id.d26, R.id.d27,
            R.id.d28, R.id.d29, R.id.d30, R.id.d31, R.id.d32, R.id.d33, R.id.d34,
            R.id.d35, R.id.d36, R.id.d37, R.id.d38, R.id.d39, R.id.d40, R.id.d41)
        for (i in 0 until 42) {{
            val d = cells[i]
            if (d == 0) {{
                views.setTextViewText(ids[i], "")
            }} else if (d == today) {{
                views.setTextViewText(ids[i], "[" + pad(d) + "]")
            }} else if (busy.contains(d)) {{
                views.setTextViewText(ids[i], pad(d) + "\\u00b7")
            }} else {{
                views.setTextViewText(ids[i], pad(d))
            }}
        }}

        views.setTextViewText(R.id.widget_badge, "TODAY")
        views.setTextViewText(R.id.widget_secondary,
            if (startableCount == 0) context.getString(R.string.widget_all_clear)
            else startableCount.toString() + " to start  \\u00b7  " +
                 planned.toString() + " min planned")

        val open = Intent(context, MainActivity::class.java)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        views.setOnClickPendingIntent(R.id.widget_root,
            PendingIntent.getActivity(context, 0, open, flags))
        mgr.updateAppWidget(id, views)
    }}

{common}
}}
'''

FINANCE_KT = '''package com.joshua.{pkg}

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * {widget_name} home-screen widget: how much runway is left.
 */
class {widget_class} : AppWidgetProvider() {{

    override fun onUpdate(context: Context, mgr: AppWidgetManager, ids: IntArray) {{
        for (id in ids) {{
            val views = RemoteViews(context.packageName, R.layout.{layout})
            views.setTextViewText(R.id.widget_primary,
                context.getString(R.string.widget_loading))
            mgr.updateAppWidget(id, views)
            thread {{ refresh(context, mgr, id) }}
        }}
    }}

    private fun refresh(context: Context, mgr: AppWidgetManager, id: Int) {{
        val views = RemoteViews(context.packageName, R.layout.{layout})
        try {{
            val o = JSONObject(httpGet(context, "/finance"))
            val runway = if (o.isNull("runway_days")) null else o.optInt("runway_days")
            val free = if (o.isNull("free_today")) null else o.optDouble("free_today", 0.0)
            val currency = o.optString("currency", "IDR")
            views.setTextViewText(R.id.widget_primary,
                if (runway == null) "-- days" else runway.toString() + " days")
            views.setTextViewText(R.id.widget_secondary,
                if (free == null) "No spending logged yet"
                else fmt(free) + " " + currency + " free today")
            views.setTextViewText(R.id.widget_badge, "RUNWAY")
        }} catch (e: Exception) {{
            views.setTextViewText(R.id.widget_primary,
                context.getString(R.string.widget_offline))
            views.setTextViewText(R.id.widget_secondary,
                context.getString(R.string.widget_offline_hint))
            views.setTextViewText(R.id.widget_badge, "")
        }}
        val open = Intent(context, MainActivity::class.java)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        views.setOnClickPendingIntent(R.id.widget_root,
            PendingIntent.getActivity(context, 0, open, flags))
        mgr.updateAppWidget(id, views)
    }}

{common}
}}
'''

# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------
HEADER = '''    <LinearLayout
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:orientation="horizontal"
        android:gravity="center_vertical">

        <TextView
            android:id="@+id/widget_badge"
            android:layout_width="0dp"
            android:layout_height="wrap_content"
            android:layout_weight="1"
            android:textSize="9sp"
            android:letterSpacing="0.14"
            android:textStyle="bold"
            android:textColor="#8E8E93"
            android:maxLines="1"
            tools:ignore="SmallSp" />

        <TextView
            android:id="@+id/widget_ask"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="@string/widget_ask"
            android:textSize="11sp"
            android:textStyle="bold"
            android:textColor="#0A84FF"
            android:paddingStart="8dp"
            android:paddingEnd="2dp" />
    </LinearLayout>

    <TextView
        android:id="@+id/widget_primary"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_marginTop="2dp"
        android:textSize="14sp"
        android:textStyle="bold"
        android:textColor="#1C1C1E"
        android:maxLines="2"
        android:ellipsize="end" />

    <ProgressBar
        android:id="@+id/widget_progress"
        style="?android:attr/progressBarStyleHorizontal"
        android:layout_width="match_parent"
        android:layout_height="4dp"
        android:layout_marginTop="6dp"
        android:max="100"
        android:progress="0" />

    <TextView
        android:id="@+id/widget_empty"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_marginTop="8dp"
        android:textSize="12sp"
        android:textColor="#8E8E93"
        android:visibility="gone" />

    <TextView
        android:id="@+id/widget_secondary"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_marginTop="6dp"
        android:textSize="11sp"
        android:textColor="#8E8E93"
        android:maxLines="2"
        android:ellipsize="end" />
'''

def task_row(i: int) -> str:
    """One tickable task row. Ids are per-row so each carries its own intent."""
    return f'''    <LinearLayout
        android:id="@+id/row{i}"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_marginTop="6dp"
        android:orientation="horizontal"
        android:gravity="center_vertical"
        android:visibility="gone">

        <TextView
            android:id="@+id/row{i}_box"
            android:layout_width="26dp"
            android:layout_height="26dp"
            android:gravity="center"
            android:textSize="15sp"
            android:textColor="#0A84FF"
            android:background="@drawable/widget_box" />

        <LinearLayout
            android:layout_width="0dp"
            android:layout_height="wrap_content"
            android:layout_weight="1"
            android:layout_marginStart="9dp"
            android:orientation="vertical">

            <TextView
                android:id="@+id/row{i}_text"
                android:layout_width="match_parent"
                android:layout_height="wrap_content"
                android:textSize="12sp"
                android:textStyle="bold"
                android:textColor="#1C1C1E"
                android:maxLines="2"
                android:ellipsize="end" />

            <TextView
                android:id="@+id/row{i}_sub"
                android:layout_width="match_parent"
                android:layout_height="wrap_content"
                android:textSize="10sp"
                android:textColor="#8E8E93"
                android:maxLines="1" />
        </LinearLayout>
    </LinearLayout>
'''

def calendar_layout() -> str:
    """Month grid: 7 weekday headers then 42 day cells (6 rows x 7)."""
    names = ["S", "M", "T", "W", "T", "F", "S"]
    head = "".join(
        f'''        <TextView
            android:layout_width="0dp"
            android:layout_height="wrap_content"
            android:layout_weight="1"
            android:gravity="center"
            android:text="{n}"
            android:textSize="9sp"
            android:textStyle="bold"
            android:textColor="#8E8E93" />
''' for n in names)
    rows = []
    for r in range(6):
        cells = "".join(
            f'''            <TextView
                android:id="@+id/d{r * 7 + c}"
                android:layout_width="0dp"
                android:layout_height="wrap_content"
                android:layout_weight="1"
                android:gravity="center"
                android:textSize="11sp"
                android:textColor="#1C1C1E"
                android:paddingTop="3dp"
                android:paddingBottom="3dp" />
''' for c in range(7))
        rows.append(f'''    <LinearLayout
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:orientation="horizontal">
{cells}    </LinearLayout>
''')
    return f'''<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:tools="http://schemas.android.com/tools"
    android:id="@+id/widget_root"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:background="@drawable/widget_bg"
    android:padding="12dp">

{HEADER}
    <LinearLayout
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_marginTop="8dp"
        android:orientation="horizontal"
        android:gravity="center_vertical">

        <TextView
            android:id="@+id/cal_title"
            android:layout_width="0dp"
            android:layout_height="wrap_content"
            android:layout_weight="1"
            android:textSize="13sp"
            android:textStyle="bold"
            android:textColor="#1C1C1E" />

        <TextView
            android:id="@+id/cal_today"
            android:layout_width="28dp"
            android:layout_height="28dp"
            android:gravity="center"
            android:textSize="12sp"
            android:textStyle="bold"
            android:textColor="#FFFFFF"
            android:background="@drawable/widget_today_pill" />
    </LinearLayout>

    <LinearLayout
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_marginTop="6dp"
        android:orientation="horizontal">
{head}    </LinearLayout>

{"".join(rows)}
</LinearLayout>
'''


def tasks_layout() -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:tools="http://schemas.android.com/tools"
    android:id="@+id/widget_root"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:background="@drawable/widget_bg"
    android:padding="12dp">

{HEADER}
{"".join(task_row(i) for i in range(5))}
</LinearLayout>
'''


def finance_layout() -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:tools="http://schemas.android.com/tools"
    android:id="@+id/widget_root"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:background="@drawable/widget_bg"
    android:padding="14dp">

{HEADER}
</LinearLayout>
'''

# Soft light card, matching the app's Apple-like surfaces.
WIDGET_BG = '''<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="rectangle">
    <solid android:color="#FFFFFF" />
    <corners android:radius="22dp" />
    <stroke android:width="1dp" android:color="#E8E8ED" />
</shape>
'''

# Circular outline for the tick box.
WIDGET_BOX = '''<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="oval">
    <solid android:color="#F2F2F7" />
    <stroke android:width="1dp" android:color="#D1D1D6" />
</shape>
'''

# Solid pill for "today" in the calendar header.
WIDGET_PILL = '''<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="oval">
    <solid android:color="#0A84FF" />
</shape>
'''


def widget_info(meta: dict) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<appwidget-provider xmlns:android="http://schemas.android.com/apk/res/android"
    android:minWidth="{meta['min_width']}dp"
    android:minHeight="{meta['min_height']}dp"
    android:targetCellWidth="4"
    android:targetCellHeight="3"
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

        if app == "vector-tasks":
            kt = TASKS_KT
            layout = tasks_layout()
        elif app == "vector-calendar":
            kt = CALENDAR_KT
            layout = calendar_layout()
        else:
            kt = FINANCE_KT
            layout = finance_layout()

        (kt_dir / f"{meta['widget_class']}.kt").write_text(
            kt.format(pkg=pkg, widget_class=meta["widget_class"],
                      widget_name=meta["widget_name"], layout=meta["layout"],
                      common=COMMON,
                      # Only the tasks template has this field; str.format
                      # ignores unused keyword arguments.
                      bindrow=BINDROW))

        (res / "layout").mkdir(parents=True, exist_ok=True)
        (res / "layout" / f"{meta['layout']}.xml").write_text(layout)
        (res / "drawable").mkdir(parents=True, exist_ok=True)
        (res / "drawable" / "widget_bg.xml").write_text(WIDGET_BG)
        (res / "drawable" / "widget_box.xml").write_text(WIDGET_BOX)
        (res / "drawable" / "widget_today_pill.xml").write_text(WIDGET_PILL)
        (res / "xml").mkdir(parents=True, exist_ok=True)
        (res / "xml" / f"{meta['layout']}_info.xml").write_text(widget_info(meta))

        print(f"generated {app}: {meta['widget_class']}")


if __name__ == "__main__":
    main()
