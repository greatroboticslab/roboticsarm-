"""
[WIRED] Session = the calendar day.

WHY THIS FILE EXISTS
---------------------
Every capture belongs to a "session" — per the plan, a session is just
today's date. This intentionally does NOT try to be "one session per
app run": if you start/stop the app five times in one day, all five
runs' captures land under the same session, because get_or_create_
today_session() always resolves to the same _id for the same date.

This is a thin, separate module (rather than logic buried in
mongo_client.py or capture_pipeline.py) so "what counts as a session"
can change later (e.g. a manual "start new session" button, or a
session that spans a physical batch instead of a day) by editing only
this file.
"""

from __future__ import annotations

from datetime import datetime

from vision.storage import mongo_client


def today_session_id() -> str:
    """The session id for 'right now' — just today's date, YYYY-MM-DD.
    Deliberately not a UUID: it needs to be *predictable* so repeated
    calls across separate app runs on the same day resolve to the same
    session without a lookup."""
    return datetime.now().strftime("%Y-%m-%d")


def get_or_create_today_session() -> str:
    """
    Ensures a `sessions` document exists for today, creating one with
    `started_at` set to now on first call of the day. Safe to call on
    every single capture — cheap upsert, no-op after the first call of
    the day. Returns the session_id to attach to the capture.
    """
    session_id = today_session_id()
    mongo_client.upsert_session_started(session_id)
    return session_id


def close_session(session_id: str | None = None) -> None:
    """Stamps `ended_at` on a session (defaults to today's). Optional —
    nothing downstream requires a session to ever be explicitly closed;
    this is just for a nicer "session ran from X to Y" display later."""
    mongo_client.update_session_ended(session_id or today_session_id())
