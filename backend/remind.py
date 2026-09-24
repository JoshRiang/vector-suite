#!/usr/bin/env python3
"""Send phone notifications for reminders that are due right now.

The user chose "phone notification from the app only" for reminders, so this
does NOT post to Telegram: it pushes to the device over FCM (or, if no device is
registered, writes to a local outbox so the omission is visible rather than
silent).

Runs every minute from cron. Each reminder fires once, because a task's lead
times produce a single one-minute window (see scheduler.due_reminders).

Usage:
  remind.py --check    # print what would fire, send nothing
"""
from __future__ import annotations

import json
import os
import sys
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


def push(title: str, body: str) -> bool:
    """Deliver one notification to the phone. Returns True if it was delivered.

    FCM is attempted only when credentials exist; otherwise the notification is
    written to the outbox and reported as undelivered, so a missing device shows
    up as a fact rather than a silent no-op.
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
                return r.status == 200
        except Exception as exc:  # noqa: BLE001
            print(f"  push failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return False

    os.makedirs(os.path.dirname(OUTBOX), exist_ok=True)
    with open(OUTBOX, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"title": title, "body": body,
                             "at": datetime.now().isoformat()}) + "\n")
    return False


def main() -> int:
    check_only = "--check" in sys.argv
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
