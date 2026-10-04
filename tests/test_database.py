"""CRUD tests against a real in-memory SQLite database."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pytest

import database as db_api


NOW = 1_700_000_000.0


async def test_init_db_creates_schema(db):
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row["name"] for row in await cursor.fetchall()}
    assert {"sessions", "active"} <= tables


async def test_create_and_finish_session(db):
    session_id = await db_api.create_session(db, "Math", NOW)
    assert session_id == 1

    cursor = await db.execute("SELECT subject, start_ts, end_ts FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    assert row["subject"] == "Math"
    assert row["end_ts"] is None

    await db_api.finish_session(db, session_id, NOW + 3600)
    cursor = await db.execute("SELECT end_ts FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    assert row["end_ts"] == NOW + 3600


async def test_get_day_totals_groups_by_subject(db):
    today = date.today()
    now = time.time()

    for subject, seconds in [("Math", 1800), ("Math", 600), ("Code", 3600)]:
        sid = await db_api.create_session(db, subject, now - seconds)
        await db_api.finish_session(db, sid, now)

    totals = await db_api.get_day_totals(db, today)
    assert totals == pytest.approx({"Math": 2400.0, "Code": 3600.0})


async def test_get_day_totals_subtracts_paused_time(db):
    now = time.time()
    sid = await db_api.create_session(db, "Reading", now - 1000)
    await db.execute("UPDATE sessions SET paused_sec = 400 WHERE id = ?", (sid,))
    await db_api.finish_session(db, sid, now)

    totals = await db_api.get_day_totals(db, date.today())
    assert totals["Reading"] == pytest.approx(600.0)


async def test_get_day_totals_excludes_other_days_and_open_sessions(db):
    now = time.time()
    yesterday = datetime.fromtimestamp(now).date() - timedelta(days=1)

    sid = await db_api.create_session(db, "Math", now - 86_400)
    await db_api.finish_session(db, sid, now - 86_400 + 600)     # finished yesterday
    await db_api.create_session(db, "Code", now)                  # still open today

    assert await db_api.get_day_totals(db, date.today()) == {}
    assert (await db_api.get_day_totals(db, yesterday)).get("Math") == pytest.approx(600.0)


async def test_active_lifecycle(db):
    sid = await db_api.create_session(db, "Language", NOW)
    await db_api.start_active(db, 42, sid, "Language", NOW)

    active = await db_api.get_active(db, 42)
    assert active == {
        "chat_id": 42, "session_id": sid, "subject": "Language",
        "started_at": NOW, "paused_sec": 0.0, "paused_at": None,
    }

    await db_api.set_paused(db, 42, NOW + 100, 0)               # pause
    assert (await db_api.get_active(db, 42))["paused_at"] == NOW + 100

    await db_api.set_paused(db, 42, None, 55)                   # resume with accrued
    resumed = await db_api.get_active(db, 42)
    assert resumed["paused_at"] is None and resumed["paused_sec"] == 55

    await db_api.clear_active(db, 42)
    assert await db_api.get_active(db, 42) is None


async def test_start_active_replaces_previous_row(db):
    """Upsert semantics: one live timer per chat, ever."""
    sid1 = await db_api.create_session(db, "Math", NOW)
    await db_api.start_active(db, 42, sid1, "Math", NOW)
    sid2 = await db_api.create_session(db, "Code", NOW + 10)
    await db_api.start_active(db, 42, sid2, "Code", NOW + 10)

    cursor = await db.execute("SELECT COUNT(*) AS n FROM active WHERE chat_id = 42")
    assert (await cursor.fetchone())["n"] == 1
    active = await db_api.get_active(db, 42)
    assert active["subject"] == "Code" and active["session_id"] == sid2
