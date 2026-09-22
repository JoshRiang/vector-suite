# VECTOR Suite

Three Android apps + one private database, for one specific problem:

> "I'm not productive, why? Because I have too many to-do lists, and I don't
> know where to start, so it leads to nothing started."

## The premise

The problem is **not** a missing to-do list. A list of 20 undifferentiated
items is what *causes* the paralysis. So this system inverts the normal model:

**You type one end goal. The system produces the work.**

You never write a task. You never prioritise. You never decide what to do
next. You state an outcome ("get a quant internship in Germany"), and the
assistant returns a dependency-ordered plan whose **first step is always
something you can start in under 30 minutes**.

## What is in here

| Component | What it does |
|---|---|
| `backend/` | One API. Turns a goal into a plan; serves all three apps. |
| `vector-tasks` | The core app. One goal in, one next action out. |
| `vector-calendar` | Today's plan: what's startable, what's done, focus time. |
| `vector-finance` | Balance, burn rate, runway. |
| `supabase/schema.sql` | The private database schema. |

All three apps share one database. A task created in `vector-tasks` appears in
`vector-calendar`; spending logged in `vector-finance` informs the day. That
integration is the reason the database is shared and not three separate ones.

## Why there is a backend at all

Two reasons, both non-negotiable:

1. **The model key can never ship in an APK.** Anything embedded in an APK is
   extractable with `unzip` + `strings`. So no app ever calls a model
   directly — the backend does.
2. **The plan must be trustworthy.** Goal decomposition is the entire product
   value, so it lives in one place that can be tested, versioned and repaired,
   instead of being duplicated in three apps.

## The design decision that matters

The single most important behaviour is in `startable_tasks`:

```sql
where t.status in ('todo','doing')
  and (t.blocked_by is null or b.status = 'done')
```

A task whose blocker is unfinished is **never** returned to the app. The user
sees exactly one thing to do. This is not a UI filter — it is enforced at the
data layer, because a UI that merely hides work still leaves the user with the
whole list in their head.

## Running it

```bash
# Backend (no dependencies beyond the stdlib for the local store)
cd backend
python3 api.py            # serves 0.0.0.0:8790

# Or as a service
systemctl --user start vector-suite-api
```

```bash
# Apps
cd vector-tasks
flutter pub get
flutter test
flutter build apk --release --target-platform android-arm64 --split-per-abi
```

The apps default to the server's Tailscale address. Override at build time:

```bash
flutter build apk --dart-define=API_BASE=http://<host>:8790
```

## Storage

`backend/store.py` implements a PostgREST-compatible `db_request()` over local
SQLite, so the system works with **zero** cloud setup. Point `SUPABASE_URL`
and `SUPABASE_SERVICE_KEY` at a Supabase project and the same calls hit
Postgres instead — no handler changes. `/health` reports which is active.

## Tests

```bash
python3 run_all_tests.py
```

Covers store semantics, API routing, the decomposition parser, the timezone
boundary, and structural checks on all three Flutter apps. No network needed.

## Privacy

Every table is row-level-security gated on `auth.uid()`. The anon key alone
reads nothing. The backend runs on the owner's own server; the database is
private to one user.

## Not a licensed advisor

`vector-finance` computes and reports. It does not recommend trades or
investments.
