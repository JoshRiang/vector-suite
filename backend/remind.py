#!/usr/bin/env python3
"""Send phone notifications for reminders that are due right now.

Delivery order: FCM device push when credentials exist, then Telegram, then a
local outbox so an undelivered reminder is visible rather than silent. Telegram
is the working channel today - the app-push path needs a Firebase project and a
device token from the app, and without a fallback the feature delivered nothing
at all.

Runs every minute from cron. Each reminder fires once, because a task's lead
times produce a single one-minute window (see scheduler.due_reminders).

Usage:
  remind.py --check    # print what would fire, send nothing
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

for _cand in (os.path.join(HERE, "..", ".env"), os.path.join(HERE, ".env")):
    if os.path.exists(_cand):
        for _line in open(_cand, encoding="utf-8"):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

import scheduler  # noqa: E402

USER_ID = "josh"
OUTBOX = os.path.join(HERE, "..", "data", "reminder_outbox.jsonl")
SENT_LOG = os.path.join(HERE, "..", "data", "reminders_sent.jsonl")
LOCK = os.path.join(HERE, "..", "data", ".remind.lock")


def _claim_lock() -> bool:
    """Take an exclusive lock so two runs cannot both deliver one reminder.

    The cron ticks every minute and a manual run (or a slow delivery) can
    overlap it. Both would read the sent-ledger before either wrote to it, so
    the same reminder was delivered twice - observed as two ledger entries
    0.36s apart for one reminder. O_EXCL is atomic, so only one process wins.
    """
    try:
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # A crashed run must not wedge reminders forever: expire a stale lock.
        try:
            age = time.time() - os.path.getmtime(LOCK)
            if age > 300:
                os.remove(LOCK)
                return _claim_lock()
        except OSError:
            pass
        return False


def _release_lock() -> None:
    try:
        os.remove(LOCK)
    except OSError:
        pass


def already_sent(task_id: str, lead: int, at: str) -> bool:
    """True if this exact reminder has fired before.

    due_reminders() has a one-minute window, but cron can run twice in the same
    minute (a retry, a manual run), and a duplicate reminder is a real annoyance
    on a phone. The (task, lead, time) triple is the identity of one reminder.
    """
    key = f"{task_id}|{lead}|{at}"
    try:
        with open(SENT_LOG, encoding="utf-8") as fh:
            for line in fh:
                try:
                    if json.loads(line).get("key") == key:
                        return True
                except ValueError:
                    continue
    except OSError:
        return False
    return False


def mark_sent(task_id: str, lead: int, at: str) -> None:
    os.makedirs(os.path.dirname(SENT_LOG), exist_ok=True)
    with open(SENT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": f"{task_id}|{lead}|{at}",
                             "sent_at": datetime.now().isoformat()}) + "\n")


def telegram(title: str, body: str) -> bool:
    """Deliver one notification over Telegram. Returns True if it was delivered.

    The app-push path needs a Firebase project and a device token from the app;
    neither exists, so reminders delivered ZERO and the user - who ranked
    reminders as the single most important feature - got nothing at all. The
    Telegram gateway is already authenticated and working, so it is used as a
    fallback rather than leaving the feature inert.

    The message is prefixed so a reminder is distinguishable from a chat reply.
    """
    import subprocess
    text = f"⏰ {title}\n{body}"
    try:
        proc = subprocess.run(
            ["hermes", "send", "--to", "telegram", text],
            capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"  telegram failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        print(f"  telegram failed: {err[-1] if err else 'no output'}",
              file=sys.stderr)
        return False
    return True


def push(title: str, body: str) -> bool:
    """Deliver one notification. Returns True if it was delivered.

    Tries FCM first when credentials exist, then Telegram. Only when BOTH fail
    is the notification written to the outbox and reported as undelivered, so a
    missing delivery channel shows up as a fact rather than a silent no-op.
    """
    server_key = os.environ.get("FCM_SERVER_KEY")
    device_token = os.environ.get("FCM_DEVICE_TOKEN")
    if server_key and device_token:
        import urllib.error
        import urllib.request
        payload = json.dumps({
            "to": device_token,
            "notification": {"title": title, "body": body, "sound": "default"},
            "priority": "high",
        }).encode()
        req = urllib.request.Request(
            "https://fcm.googleapis.com/fcm/send", data=payload,
            headers={"Authorization": f"key={server_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status == 200:
                    return True
        except Exception as exc:  # noqa: BLE001
            print(f"  push failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        # Fall through to Telegram rather than dropping the reminder.

    if telegram(title, body):
        return True

    os.makedirs(os.path.dirname(OUTBOX), exist_ok=True)
    with open(OUTBOX, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"title": title, "body": body,
                             "at": datetime.now().isoformat()}) + "\n")
    return False


def main() -> int:
    check_only = "--check" in sys.argv
    if not check_only and not _claim_lock():
        # Another run holds the lock. Skipping is correct: the holder will see
        # the same due reminders, and delivering twice is worse than a delay of
        # one tick.
        return 0
    try:
        return _run(check_only)
    finally:
        if not check_only:
            _release_lock()


def _run(check_only: bool) -> int:
    due = scheduler.due_reminders(USER_ID)
    if not due:
        return 0

    delivered = 0
    for item in due:
        title = item["title"]
        lead = int(item["lead_minutes"])
        at = item["at"]
        if already_sent(item["id"], lead, at):
            continue
        when = item.get("when") or ""
        body = f"{when} — starts {at[11:16]}"
        if lead >= 1440:
            body = f"Tomorrow: {title} ({at[11:16]})"
        print(f"  due: {title} | {when} | lead={lead}m")
        if check_only:
            continue
        if push(title, body):
            delivered += 1
        # Marked sent either way: retrying a failed push forever would spam the
        # log, and the outbox records what was not delivered.
        mark_sent(item["id"], lead, at)

    if not check_only:
        print(f"  {len(due)} due, {delivered} delivered to the phone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
