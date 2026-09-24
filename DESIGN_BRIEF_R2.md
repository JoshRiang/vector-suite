# VECTOR apps — round 2: what the user actually complained about

His words, verbatim, when asked what was wrong:

> "the widget is so bad, there are connection issue, the calendar DIDNT LOOK like
> calendar at all, the tasks didnt have checklist on widget, the calendar now
> only show dates not hours (add detailed hours by hours for the calendar), make
> the task actually look like checklist with the priority is set by you"

And on how he wants to interact:

> "Only Telegram — the app is just for viewing"

So: **the apps become viewers.** Telegram is where he instructs. The command bar
is REMOVED. Everything below is about making the viewer excellent.

## HARD ENVIRONMENT CONSTRAINTS — unchanged

- **No Flutter, Dart, Java or Android SDK on this machine.** Cannot build,
  cannot `dart analyze`. CI (GitHub Actions) is the only compiler.
- Verify with: `python3 /home/josh/vector_suite/check_apps.py`,
  `python3 /home/josh/vector_suite/check_late_final.py`,
  `python3 /home/josh/vector_suite/verify_dart_static.py`
  All three must pass for your app.
- **Do NOT commit or push.** Leave changes in the working tree.
- `lib/api_client.dart` must stay byte-identical to
  `/home/josh/vector_suite/shared_dart/api_client.dart`.

## Dart pitfalls that have already caused a blank screen — do not reintroduce

1. Never assign a `late final` field twice → use plain `late`.
2. No `as List` / `as Map` / `as num` / `as String` casts on API data (only
   allowed when an `is` check narrows it first). Use `is` checks.
3. Cupertino-only: no `package:flutter/material.dart`, no `Colors.*`, no
   `RefreshIndicator`, no `ListView`/`GridView`. Use `CupertinoColors`,
   `CupertinoSliverRefreshControl`, `CustomScrollView` + `SliverList`.
4. `library;` must precede every import.
5. No `const Duration(<runtime value>)`.
6. Guard `.first` / `[0]` with an `isNotEmpty` check.
7. A class-scoped `static const` used from ANOTHER class fails to compile
   (this broke CI once). Put shared constants at file scope.
8. Add `// ignore_for_file: use_build_context_synchronously` at the top.

## Backend endpoints available (all verified live)

- `GET /calendar/range?start=YYYY-MM-DD&end=YYYY-MM-DD`
  → `{today, start, end, items:[…], days:[{date, count, items:[…]}]}`
  item: `{id, title, why, minutes, status, priority, scheduled_at, completed_at,
  all_day, location, notes, goal_id, color, auto_scheduled}`
  - `scheduled_at` = `"YYYY-MM-DDTHH:MM:SS"`, or null (undated)
  - `priority` 1 = most important, 5 = least
  - `status` ∈ `todo` | `doing` | `done`
  - `auto_scheduled` 1 = the scheduler chose this time (movable); 0 = user set it
- `GET /schedule?date=YYYY-MM-DD` → preview of the day's plan (writes nothing):
  `{date, planned:[…], moved:[…], fixed_count, skipped:[{id,title,reason}],
  planned_count, planned_minutes, unscheduled_minutes, day_start, day_end}`
- `GET /reminders` → `{due:[{id,title,at,lead_minutes,when}]}`
- `GET /goals` → rows keyed **`goal_id`** (not `id`), with `total_tasks`,
  `done_tasks`, `pct_done`
- `GET /goals/<goal_id>/tasks` → `{goal_id, tasks:[{id,title,why,minutes,status,
  priority,scheduled_at,startable}], total, done, pct_done}`
- `GET /today` → `{date, startable:[…], done_today:[…], focus_minutes, sessions}`

Client methods already exist for all of the above — see
`shared_dart/api_client.dart`. Do not add new ones.

## APP 1 — vector-calendar: must look like a real calendar

File: `/home/josh/vector_suite/vector-calendar/lib/main.dart`

The core complaint: **"only show dates not hours"**. A calendar that lists dates
is not a calendar.

### Day view — hour-by-hour grid (the main fix)
- A vertical time grid, **05:00 → 24:00** (20 rows, one per hour). The user chose
  this range explicitly.
- A left gutter showing hour labels (`05:00`, `06:00`, … `23:00`).
- Horizontal rules per hour so the grid reads as a calendar.
- Each timed item is drawn as a block at its hour, sized by its duration, with
  title, time range (`09:00–09:20`) and its priority marker. Overlapping items
  must be visually distinguishable, not drawn on top of each other.
- **A live "now" indicator** on today: a line at the current time.
- All-day / undated items pinned in a strip ABOVE the grid.
- Scrolling to ~07:00 on open rather than starting at the top.
- Empty hours stay visible — the grid must always look like a grid.

### Month view
- Real month grid, 7 columns, weekday headers, leading blanks, today marked.
- A dot on days that have items (from `/calendar/range` for the visible month).
- Tapping a day opens that day's hour grid.

### Also
- Swipe left/right to change day.
- Pull-to-refresh: `CupertinoSliverRefreshControl`.
- **REMOVE the command bar and its exchange list entirely** — Telegram is the
  way he instructs now.
- Read-only: no add/edit/delete UI. Viewing only.
- Honest empty states ("Nothing scheduled").

## APP 2 — vector-tasks: must look like a checklist

File: `/home/josh/vector_suite/vector-tasks/lib/main.dart`

### Checklist rows
- A real checkbox per row (`CupertinoCheckbox` or a drawn circle/square) that
  reads as a checklist, not a card.
- Completed: checkbox filled + title struck through.
- Subtitle: due date/time and duration.
- **A priority marker on every row** — the priority comes from the server and
  must be visible. Use colour plus a label (`P1`…`P5` or High/Med/Low), since
  colour alone is not readable for everyone.
- Tapping the checkbox completes (`completeTask`) / un-completes (`reopenTask`).
  Tapping the row opens detail.
- Grouping: `Overdue / Today / Tomorrow / Later / No date`.
- A "Hide completed" toggle.

### Structure
- Lists = the existing goals (`goals()`, key `goal_id`), plus an "All" view and a
  "No list" chip for dated tasks with no goal.
- **REMOVE the command bar and its exchange list** — Telegram is the way.
- Read-only apart from the checkbox toggle and hide-completed. No add/edit/delete
  UI; the scheduler and Telegram own that now.
- Pull-to-refresh: `CupertinoSliverRefreshControl`.

## Design language — KEEP IT (he likes the design)

`AppColors` already in each `main.dart`:
`bgBase 0xFFF5F5F7`, `bgTop 0xFFEEF1FF`, `glass 0xCCFFFFFF`, `accent 0xFF6366F1`,
`accentSoft 0xFF8B5CF6`, `success 0xFF10B981`, `danger 0xFFEF4444`,
`warning 0xFFF59E0B`, `textPrimary 0xFF1C1C1E`, `textSecondary 0xFF6B7280`,
`textTertiary 0xFF9CA3AF`.
Keep the frosted glass cards, generous spacing, large bold headings, gradient
background, Apple-like feel.

## Widgets — native Kotlin, generated by gen_widgets.py

The user: **"the tasks didnt have checklist on widget"** and **"the widget is so
bad, there are connection issue"**.

### Tasks widget must be a checklist
- Each row: a real checkbox glyph reflecting state (done vs not done), the task
  title, and its **priority marker**.
- A progress line: `N/M done` plus a progress bar.
- Tapping a row's checkbox completes it and refreshes the widget.
- Priority must be visible per row (colour + short label).

### Connection reliability
- Every network call already loops over `candidateBases` (public → LAN →
  tailnet). Keep that, and keep the beacon.
- Distinguish a transport failure (`java.io.IOException` → "Server unreachable")
  from a code bug (`Widget bug: …`) — a widget bug mislabelled as a network
  fault sends the diagnosis after a fault that does not exist.
- Never show a bare "Loading…" forever: on failure show the last known state or
  an explicit message.
- The widget must still render the grid/checklist when offline, just without
  live data.

## Definition of done

- All three checkers pass for your app
- No Material imports, no unguarded casts, no double-assigned `late final`
- No command bar anywhere
- Hour grid present in the calendar day view (grep-able: hour labels 05:00–23:00)
- `api_client.dart` byte-identical to the shared copy
- Changes left UNCOMMITTED

Report: files changed, exact checker output, and anything you could not do.
