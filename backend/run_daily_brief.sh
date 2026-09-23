#!/usr/bin/env bash
# Run the VECTOR Suite daily brief against whatever database backend is
# configured, then print it to stdout.
#
# WHY a wrapper instead of inlining this in the cron prompt:
#   The DSN in backend/.env contains shell-significant characters (`*`, `,`) and
#   a percent-encoded `#`. Sourcing it correctly depends on the calling shell's
#   parsing rules, which makes a cron prompt fragile. One script keeps the
#   quoting in one reviewable place.
#
# The brief reads the store directly, so it needs no API key.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

# .env holds DATABASE_URL. Sourcing it (rather than hardcoding) means the brief
# follows the API to Postgres automatically, and falls back to SQLite if the DSN
# is ever removed.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

exec python3 daily_brief.py
