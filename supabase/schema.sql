-- ============================================================================
-- VECTOR SUITE — shared private database schema
-- ============================================================================
-- One private Postgres database backing three apps:
--   vector-tasks    (end goal -> AI-generated to-do list)
--   vector-calendar (day schedule, AI-planned blocks)
--   vector-finance  (budget / runway)
--
-- WHY a shared DB: the three apps are one system. A task created in
-- vector-tasks gets a time block in vector-calendar; a finance decision
-- depends on what the day actually costs. Separate databases would make
-- that integration impossible.
--
-- PRIVACY: every table is row-level-security gated on auth.uid(). No row is
-- readable by another user, and the anon key alone can read nothing.
--
-- Run this once in the Supabase SQL editor.
-- ============================================================================

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- GOALS — the ONLY thing the user types by hand.
-- The whole product premise: user states an end goal, the system derives the
-- work. Every task traces back to a goal so progress is measurable.
-- ---------------------------------------------------------------------------
create table if not exists goals (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  title        text not null,
  detail       text,
  target_date  date,
  status       text not null default 'active'
               check (status in ('active','done','paused','dropped')),
  created_at   timestamptz not null default now(),
  completed_at timestamptz
);
create index if not exists goals_user_status_idx on goals(user_id, status);

-- ---------------------------------------------------------------------------
-- TASKS — derived from a goal by the decomposition engine.
-- `blocked_by` is a self-reference so the app can surface only the tasks that
-- are actually startable, instead of dumping the whole list and recreating
-- the paralysis the product exists to remove.
-- ---------------------------------------------------------------------------
create table if not exists tasks (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  goal_id      uuid references goals(id) on delete cascade,
  title        text not null,
  why          text,                       -- engine's one-line rationale
  minutes      int  not null default 30 check (minutes between 5 and 480),
  blocked_by   uuid references tasks(id) on delete set null,
  status       text not null default 'todo'
               check (status in ('todo','doing','done','skipped')),
  priority     int  not null default 3 check (priority between 1 and 5),
  source       text not null default 'ai' check (source in ('ai','user')),
  scheduled_at timestamptz,               -- set when placed on the calendar
  created_at   timestamptz not null default now(),
  completed_at timestamptz
);
create index if not exists tasks_user_status_idx  on tasks(user_id, status);
create index if not exists tasks_goal_idx         on tasks(goal_id);
create index if not exists tasks_scheduled_idx    on tasks(user_id, scheduled_at);

-- ---------------------------------------------------------------------------
-- DAY PLAN — one row per user per day.
-- Stores the assistant's proposed plan separately from what actually
-- happened, so the user can see "planned vs actual" (the productivity
-- measurement) without the plan being overwritten by reality.
-- ---------------------------------------------------------------------------
create table if not exists day_plans (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  plan_date    date not null,
  planned_minutes int not null default 0,
  actual_minutes  int not null default 0,
  summary      text,                       -- assistant's one-line read of the day
  created_at   timestamptz not null default now(),
  unique(user_id, plan_date)
);
create index if not exists day_plans_user_date_idx on day_plans(user_id, plan_date);

-- ---------------------------------------------------------------------------
-- FOCUS SESSIONS — the productivity measurement.
-- Deliberately minimal: start, stop, and whether the task got done. The
-- score is derived from these rows, never stored as a mutable counter (a
-- counter can drift from reality; a log cannot).
-- ---------------------------------------------------------------------------
create table if not exists focus_sessions (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  task_id      uuid references tasks(id) on delete set null,
  started_at   timestamptz not null default now(),
  ended_at     timestamptz,
  minutes      int,
  completed    boolean not null default false
);
create index if not exists focus_user_started_idx on focus_sessions(user_id, started_at);

-- ---------------------------------------------------------------------------
-- FINANCE — mirrors the existing Vector planner, now server-backed so the
-- numbers survive a reinstall and the calendar can see real daily burn.
-- ---------------------------------------------------------------------------
create table if not exists finance_settings (
  user_id       uuid primary key references auth.users(id) on delete cascade,
  currency      text not null default 'IDR',
  balance       numeric(14,2) not null default 0,
  daily_budget  numeric(14,2) not null default 0,
  target_date   date,
  monthly_income numeric(14,2) not null default 0,
  updated_at    timestamptz not null default now()
);

create table if not exists expenses (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  amount      numeric(14,2) not null check (amount > 0),
  note        text,
  category    text,
  spent_on    date not null default current_date,
  created_at  timestamptz not null default now()
);
create index if not exists expenses_user_date_idx on expenses(user_id, spent_on);

-- ---------------------------------------------------------------------------
-- ROW LEVEL SECURITY
-- Without this the anon key would expose every row to anyone holding it.
-- The policy is uniform: you can only touch rows where user_id = auth.uid().
-- ---------------------------------------------------------------------------
alter table goals            enable row level security;
alter table tasks            enable row level security;
alter table day_plans        enable row level security;
alter table focus_sessions   enable row level security;
alter table finance_settings enable row level security;
alter table expenses         enable row level security;

do $$
declare t text;
begin
  foreach t in array array['goals','tasks','day_plans','focus_sessions','finance_settings','expenses']
  loop
    execute format('drop policy if exists %I_owner on %I', t, t);
    execute format(
      'create policy %I_owner on %I for all
         using (auth.uid() = user_id)
         with check (auth.uid() = user_id)', t, t);
  end loop;
end $$;

-- ---------------------------------------------------------------------------
-- HELPER VIEWS — computed, never stored (a stored counter drifts).
-- ---------------------------------------------------------------------------

-- Only the tasks that can actually be started right now: not done, and either
-- unblocked or whose blocker is finished. This is what the home screen shows.
create or replace view startable_tasks
with (security_invoker = true) as
select t.*
from tasks t
left join tasks b on b.id = t.blocked_by
where t.status in ('todo','doing')
  and (t.blocked_by is null or b.status = 'done');

-- Per-goal progress, so "am I actually moving?" is one query.
create or replace view goal_progress
with (security_invoker = true) as
select g.id            as goal_id,
       g.user_id,
       g.title,
       g.status,
       count(t.id)                                              as total_tasks,
       count(t.id) filter (where t.status = 'done')             as done_tasks,
       coalesce(sum(t.minutes) filter (where t.status = 'done'), 0) as minutes_done,
       coalesce(sum(t.minutes), 0)                              as minutes_total,
       case when count(t.id) = 0 then 0
            else round(100.0 * count(t.id) filter (where t.status='done')
                       / count(t.id)) end                       as pct_done
from goals g
left join tasks t on t.goal_id = g.id
group by g.id, g.user_id, g.title, g.status;

-- Daily focus score: completed tasks vs planned, and minutes actually spent.
create or replace view daily_productivity
with (security_invoker = true) as
select f.user_id,
       f.started_at::date                                       as day,
       count(*)                                                 as sessions,
       coalesce(sum(f.minutes), 0)                              as minutes,
       count(*) filter (where f.completed)                      as completed
from focus_sessions f
group by f.user_id, f.started_at::date;
