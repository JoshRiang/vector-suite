-- ============================================================================
-- VECTOR SUITE — Postgres schema (Supabase)
-- ============================================================================
-- This mirrors backend/store.py's SCHEMA constant exactly, because store.py
-- runs its query builder, PostgREST filter parser and blocking logic against
-- either engine (see pgcompat.py).
--
-- WHY text timestamps instead of timestamptz:
--   The apps filter "what did I finish today" by comparing a LOCAL date to the
--   stored timestamp. timestamptz normalises to UTC on write and read, which
--   silently moved tasks completed between 00:00 and 07:00 WIB into the
--   previous day - a real bug that was found and fixed in the SQLite build
--   (test_timezone.py pins it). Storing local ISO-8601 text keeps the two
--   backends byte-identical so the same tests pass on both.
--
-- WHY no auth.users foreign key:
--   The API authenticates with a shared secret (X-Api-Key) and a stable text
--   user id ("josh"), and talks to the DB as the postgres superuser. Rows are
--   scoped by the app's own user_id column, not by Supabase auth. A FK to
--   auth.users would reject every insert, since no Supabase auth user exists.
--
-- Idempotent: safe to re-run.
-- ============================================================================

create table if not exists goals (
  id           text primary key,
  user_id      text not null,
  title        text not null,
  detail       text,
  target_date  text,
  status       text not null default 'active',
  created_at   text not null,
  completed_at text
);
create index if not exists goals_user_status_idx on goals(user_id, status);

create table if not exists tasks (
  id           text primary key,
  user_id      text not null,
  goal_id      text,
  title        text not null,
  why          text,
  minutes      integer not null default 30,
  blocked_by   text,
  status       text not null default 'todo',
  priority     integer not null default 3,
  source       text not null default 'ai',
  scheduled_at text,
  created_at   text not null,
  completed_at text
);
create index if not exists tasks_user_status_idx on tasks(user_id, status);
create index if not exists tasks_goal_idx        on tasks(goal_id);

create table if not exists day_plans (
  id              text primary key,
  user_id         text not null,
  plan_date       text not null,
  planned_minutes integer not null default 0,
  actual_minutes  integer not null default 0,
  summary         text,
  created_at      text not null,
  unique(user_id, plan_date)
);

create table if not exists focus_sessions (
  id         text primary key,
  user_id    text not null,
  task_id    text,
  started_at text not null,
  ended_at   text,
  minutes    integer,
  completed  integer not null default 0
);
create index if not exists focus_user_started_idx on focus_sessions(user_id, started_at);

create table if not exists finance_settings (
  user_id        text primary key,
  currency       text not null default 'IDR',
  balance        double precision not null default 0,
  daily_budget   double precision not null default 0,
  target_date    text,
  monthly_income double precision not null default 0,
  updated_at     text not null
);

create table if not exists expenses (
  id         text primary key,
  user_id    text not null,
  amount     double precision not null,
  note       text,
  category   text,
  spent_on   text not null,
  created_at text not null
);
create index if not exists expenses_user_date_idx on expenses(user_id, spent_on);

-- ============================================================================
-- ============================================================================
-- Chat / command console
-- ============================================================================
-- Records every instruction the user types and the reply, so a change made by
-- an instruction is traceable back to the words that caused it.
create table if not exists chat_messages (
  id         text primary key,
  user_id    text not null,
  role       text not null,
  content    text not null,
  created_at text not null
);
create index if not exists chat_user_created_idx on chat_messages(user_id, created_at);

-- Additive columns are applied by store._migrate(), NOT here. ALTER TABLE needs
-- an ACCESS EXCLUSIVE lock, so putting them in this file would block for the
-- server's statement timeout every time a second process (a cron job, a test)
-- ran init_db() while the API held the table. _migrate() sets a short
-- lock_timeout and treats an already-present column as success.

alter table chat_messages enable row level security;

-- Access control
-- ============================================================================
-- The service connects as the postgres role, which bypasses RLS. RLS is enabled
-- anyway so that if the anon/authenticated keys are ever used from a client,
-- the tables are NOT world-readable. With no policy granting access, those
-- roles get nothing.
alter table goals            enable row level security;
alter table tasks            enable row level security;
alter table day_plans        enable row level security;
alter table focus_sessions   enable row level security;
alter table finance_settings enable row level security;
alter table expenses         enable row level security;

-- Belt and braces: revoke default grants from the public API roles.
do $$
begin
  execute 'revoke all on all tables in schema public from anon, authenticated';
exception when undefined_object then
  null;  -- roles do not exist outside a full Supabase project
end $$;
