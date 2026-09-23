# VECTOR apps — build brief

Two Flutter apps must be rewritten to match Google Calendar / Google Tasks in
capability, while keeping the existing Apple-like visual language (the user
explicitly likes the design and hates generic AI-generated UI).

## HARD ENVIRONMENT CONSTRAINTS — read first

- **There is NO Flutter, Dart, Java or Android SDK on this machine.** You cannot
  run `flutter build`, `flutter test`, `dart analyze`, or `gradle`. Do not try,
  do not install them. Builds happen ONLY in GitHub Actions.
- Verify with the project's own checkers (they exist precisely for this):
  - `python3 /home/josh/vector_suite/check_apps.py`
  - `python3 /home/josh/vector_suite/check_late_final.py`
  Both must print OK for your app.
- **Do NOT commit or push.** Leave changes in the working tree.
- Each app is its own git repo nested inside `/home/josh/vector_suite`.

## Known Dart pitfalls in this codebase (all previously caused a blank screen)

1. **Never assign a `late final` field twice.** A second assignment throws
   `LateInitializationError` asynchronously and leaves the screen blank with no
   message. Use plain `late` if a field may be assigned more than once.
2. **Never use `as List` / `as num` / `as Map` casts on API data.** A wrong type
   throws inside `build()` and blanks the screen. Use `x is num ? (x as num).toInt() : 0`
   and for lists:
   `x is List ? x.whereType<Map>().map((m) => Map<String, dynamic>.from(m)).toList() : <Map<String, dynamic>>[]`
3. **The apps are Cupertino-only.** Do NOT import `package:flutter/material.dart`
   and do NOT use `Colors.*`, `RefreshIndicator`, `ListView` or `GridView`
   (Material/unsafe here). Use `CupertinoColors`, `CupertinoSliverRefreshControl`,
   and `CustomScrollView` + `SliverList`. A single Material import breaks the build.
4. **`const` with a runtime value is a compile error.** e.g. `const Duration(days: n)`.
5. **Always guard empty lists before `.first` / `[0]`.**
6. `library;` must come BEFORE all imports.
7. Add `// ignore_for_file: use_build_context_synchronously` at the top of
   `lib/main.dart` — the apps use `context` after `await` in several places.

## Shared API client

`/home/josh/vector_suite/shared_dart/api_client.dart` is the source of truth.
Each app keeps a committed copy. After any change to it, copy it in:
```
for n in vector-tasks vector-calendar vector-finance; do cp /home/josh/vector_suite/shared_dart/api_client.dart /home/josh/vector_suite/$n/lib/api_client.dart; done
```

### Available client methods (already implemented — do not reinvent)

```dart
Api({String? baseUrl, required String userId, String? apiKey, http.Client? client})
static const Api.defaultUserId
static const Api.defaultBaseUrl
static void Api.beacon(String stage, [String detail])   // fire-and-forget diagnostics

Future<List<Map<String,dynamic>>> goals()
Future<List<Map<String,dynamic>>> startable()
Future<Map<String,dynamic>>       today()
Future<Map<String,dynamic>>       productivity()
Future<Map<String,dynamic>>       finance()
Future<Map<String,dynamic>>       calendarRange({String? start, String? end})
Future<Map<String,dynamic>>       goalTasks(String goalId)
Future<Map<String,dynamic>>       createGoal(String title, {String? detail, String? targetDate})
Future<Map<String,dynamic>>       addTask(String goalId, String title, {int minutes, String? why, int priority})
Future<Map<String,dynamic>>       upsertTask({String? id, String? title, String? scheduledAt,
                                              int? minutes, bool? allDay, String? location,
                                              String? notes, String? goalId, int? priority, String? status})
Future<void>                      deleteTask(String id)
Future<void>                      completeTask(String id)
Future<void>                      reopenTask(String id)
Future<void>                      setTaskStatus(String id, String status)
Future<void>                      renameGoal(String goalId, String title)
Future<void>                      deleteGoal(String goalId)
Future<void>                      addExpense(num amount, {String? note, String? category})

Future<Map<String,dynamic>>       command(String instruction)   // natural-language instruction
Future<List<Map<String,dynamic>>> commandHistory()
```

`ApiException` has `.message` and `.offline` (bool).

## Backend response shapes (verified live — use these exact keys)

- `GET /calendar/range?start=YYYY-MM-DD&end=YYYY-MM-DD`
  → `{today, start, end, items:[...], days:[{date, count, items:[...]}]}`
  Each item: `{id, title, why, minutes, status, priority, scheduled_at,
  completed_at, all_day, location, notes, goal_id, color}`
  - `scheduled_at` is `"YYYY-MM-DDTHH:MM:SS"`; `all_day` is `0`/`1` (int) or null.
  - `status` is one of `todo` | `doing` | `done`.
  - Items with no `scheduled_at` are undated ("inbox") work.
- `GET /goals` → rows keyed **`goal_id`** (NOT `id`):
  `{goal_id, user_id, title, status, total_tasks, done_tasks, minutes_done, minutes_total, pct_done}`
- `GET /goals/<goal_id>/tasks` → `{goal_id, tasks:[{id,title,why,minutes,status,blocked_by,priority,scheduled_at,startable,blocked_by_title}], total, done, pct_done}`
- `GET /today` → `{date, startable:[...], done_today:[...], focus_minutes, sessions}`
- `POST /command` body `{"instruction": "..."}`
  → `{ok, reply, applied:[{ok, action, id?, title?, error?}], count}`
  Show `reply` prominently; when `applied` contains an entry with `error`,
  surface it — a partial failure must never look like success.

## App 1: vector-calendar  (Google Calendar-like)

File: `/home/josh/vector_suite/vector-calendar/lib/main.dart`

Required:
- **Month view**: a real month grid, 7 columns, weekday headers, leading blanks,
  today clearly marked, and a dot/indicator on every day that has items (from
  `/calendar/range` covering the visible month).
- **Day view**: tap a day → that day's agenda, items sorted by time, all-day
  items pinned at the top, each showing time, title, location and duration.
- **Create / edit an event**: a sheet or page with title, date, start time,
  duration (minutes), all-day toggle, location, notes. Save via `upsertTask`
  (pass `id` to edit). Swipe or long-press an item to delete (`deleteTask`) and
  to complete (`completeTask`).
- **Swipe between days/months** (a `PageView` over days is fine).
- **Command bar**: a text field where the user types an instruction such as
  "move my 3pm to tomorrow" or "add gym Friday 7am for an hour". Call
  `command(instruction)`, show the returned `reply`, then reload the calendar.
  Keep the recent exchange visible (a small list) so the user sees what changed.
- Pull-to-refresh must be `CupertinoSliverRefreshControl`.
- Empty states must be honest ("Nothing scheduled") not decorative.

## App 2: vector-tasks  (Google Tasks-like)

File: `/home/josh/vector_suite/vector-tasks/lib/main.dart`

Required:
- **Task lists** = the existing goals (use `goals()`, key `goal_id`).
  Sidebar or segmented control to switch lists, plus a "All tasks" view.
- **Task rows**: a checkbox that completes (`completeTask`) and un-completes
  (`reopenTask`), the title, and a subtitle line showing due date / time and
  minutes. Completed tasks show struck-through and can be hidden with a toggle.
- **Add a task**: a field at the bottom (like Google Tasks) with optional
  date+time picker, notes and priority. Use `addTask` for a goal task or
  `upsertTask` for a dated one.
- **Task detail**: tap a row → edit title, date, time, all-day, notes, and the
  list it belongs to. Save with `upsertTask` (pass `id`).
- **Sort/group**: by date (Overdue / Today / Tomorrow / Later / No date).
- **Command bar**: same as the calendar — natural-language instruction via
  `command()`, show `reply`, then reload. This is the point of the product: the
  user manages the list by talking to Hermes through the app.
- Pull-to-refresh must be `CupertinoSliverRefreshControl`.

## Design language (keep it — the user likes it)

Existing palette, already in `lib/main.dart` as `AppColors`:
`bgBase 0xFFF5F5F7`, `bgTop 0xFFEEF1FF`, `glass 0xCCFFFFFF`, `accent 0xFF6366F1`,
`accentSoft 0xFF8B5CF6`, `success 0xFF10B981`, `danger 0xFFEF4444`,
`warning 0xFFF59E0B`, `textPrimary 0xFF1C1C1E`, `textSecondary 0xFF6B7280`,
`textTertiary 0xFF9CA3AF`.
Keep the frosted "glass" cards, generous spacing, large bold headings, and the
gradient background. Preserve any existing helper widgets (e.g. `_glass`).

## Definition of done

- `python3 /home/josh/vector_suite/check_apps.py` → OK for your app
- `python3 /home/josh/vector_suite/check_late_final.py` → OK for your app
- No Material imports, no `as` casts on API data, no double-assigned `late final`
- `lib/api_client.dart` byte-identical to `shared_dart/api_client.dart`
- Changes left UNCOMMITTED in the working tree

Report: files changed, the exact checker output, and anything you could not do.
