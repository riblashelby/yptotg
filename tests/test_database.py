"""CRUD tests against a real in-memory SQLite database (v2: subject registry)."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pytest

import database as db_api


NOW = 1_700_000_000.0


async def _math_id(db) -> int:
    subject = await db_api.find_subject(db, "Math")
    assert subject is not None
    return subject[0]


async def test_init_db_creates_schema_and_seeds_subjects(db):
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row["name"] for row in await cursor.fetchall()}
    assert {"subjects", "sessions", "active"} <= tables

    names = [name for _, name in await db_api.list_subjects(db)]
    assert {"Math", "Code", "Reading", "Language", "Other"} <= set(names)


# --------------------------------------------------------------------------- subjects CRUD
async def test_add_subject(db):
    subject_id, clean = await db_api.add_subject(db, "  Physics   Quantum  ")
    assert clean == "Physics Quantum"
    assert await db_api.get_subject(db, subject_id) == (subject_id, "Physics Quantum")


async def test_add_subject_rejects_duplicate_case_insensitive(db):
    with pytest.raises(ValueError):
        await db_api.add_subject(db, "math")


async def test_add_subject_rejects_empty(db):
    with pytest.raises(ValueError):
        await db_api.add_subject(db, "   ")


async def test_rename_subject_updates_label_everywhere(db):
    sid, _ = await db_api.find_subject(db, "Code") or (0, "")
    renamed_id, clean = await db_api.rename_subject(db, sid, "TypeScript")
    assert (renamed_id, clean) == (sid, "TypeScript")
    assert await db_api.get_subject(db, sid) == (sid, "TypeScript")
    assert await db_api.find_subject(db, "Code") is None


async def test_rename_subject_errors(db):
    sid, _ = (await db_api.list_subjects(db))[0]
    other_sid, _ = await db_api.add_subject(db, "Chemistry")
    with pytest.raises(ValueError):          # duplicate name
        await db_api.rename_subject(db, sid, "chemistry")
    with pytest.raises(KeyError):            # unknown id
        await db_api.rename_subject(db, 99999, "Nope")
    with pytest.raises(ValueError):          # empty name
        await db_api.rename_subject(db, other_sid, " ")


# --------------------------------------------------------------------------- sessions
async def test_create_and_finish_session(db):
    math_id = await _math_id(db)
    session_id = await db_api.create_session(db, math_id, NOW)
    assert session_id == 1

    cursor = await db.execute(
        "SELECT s.name, t.start_ts, t.end_ts FROM sessions AS t "
        "JOIN subjects AS s ON s.id = t.subject_id WHERE t.id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    assert row["name"] == "Math"
    assert row["end_ts"] is None

    await db_api.finish_session(db, session_id, NOW + 3600)
    cursor = await db.execute("SELECT end_ts FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    assert row["end_ts"] == NOW + 3600


async def test_get_day_totals_groups_by_subject(db):
    today = date.today()
    now = time.time()

    ids = {name: sid for sid, name in await db_api.list_subjects(db)}
    for subject, seconds in [("Math", 1800), ("Math", 600), ("Code", 3600)]:
        sid = await db_api.create_session(db, ids[subject], now - seconds)
        await db_api.finish_session(db, sid, now)

    totals = await db_api.get_day_totals(db, today)
    assert totals == pytest.approx({"Math": 2400.0, "Code": 3600.0})


async def test_get_day_totals_follows_rename(db):
    """Rename updates the label on historical stats — display-name semantics."""
    now = time.time()
    code_id, _ = await db_api.find_subject(db, "Code")
    sid = await db_api.create_session(db, code_id, now - 1800)
    await db_api.finish_session(db, sid, now)

    await db_api.rename_subject(db, code_id, "TypeScript")
    totals = await db_api.get_day_totals(db, date.today())
    assert totals == pytest.approx({"TypeScript": 1800.0})


async def test_get_day_totals_subtracts_paused_time(db):
    now = time.time()
    reading_id, _ = await db_api.find_subject(db, "Reading")
    sid = await db_api.create_session(db, reading_id, now - 1000)
    await db_api.finish_session(db, sid, now, paused_sec=400)  # stop flow persists accrued pause

    totals = await db_api.get_day_totals(db, date.today())
    assert totals["Reading"] == pytest.approx(600.0)


async def test_get_day_totals_excludes_other_days_and_open_sessions(db):
    now = time.time()
    yesterday = datetime.fromtimestamp(now).date() - timedelta(days=1)
    ids = {name: sid for sid, name in await db_api.list_subjects(db)}

    sid = await db_api.create_session(db, ids["Math"], now - 86_400)
    await db_api.finish_session(db, sid, now - 86_400 + 600)   # finished yesterday
    await db_api.create_session(db, ids["Code"], now)           # still open today

    assert await db_api.get_day_totals(db, date.today()) == {}
    assert (await db_api.get_day_totals(db, yesterday)).get("Math") == pytest.approx(600.0)


# --------------------------------------------------------------------------- active timer
async def test_active_lifecycle(db):
    lang_id, _ = await db_api.find_subject(db, "Language")
    sid = await db_api.create_session(db, lang_id, NOW)
    await db_api.start_active(db, 42, sid, lang_id, NOW)

    active = await db_api.get_active(db, 42)
    assert active["chat_id"] == 42 and active["session_id"] == sid
    assert active["subject"] == "Language" and active["subject_id"] == lang_id
    assert active["started_at"] == NOW and active["paused_sec"] == 0.0 and active["paused_at"] is None

    await db_api.set_paused(db, 42, NOW + 100, 0)               # pause
    assert (await db_api.get_active(db, 42))["paused_at"] == NOW + 100

    await db_api.set_paused(db, 42, None, 55)                   # resume with accrued
    resumed = await db_api.get_active(db, 42)
    assert resumed["paused_at"] is None and resumed["paused_sec"] == 55

    await db_api.clear_active(db, 42)
    assert await db_api.get_active(db, 42) is None


async def test_start_active_replaces_previous_row(db):
    """Upsert semantics: one live timer per chat, ever."""
    ids = {name: sid for sid, name in await db_api.list_subjects(db)}
    sid1 = await db_api.create_session(db, ids["Math"], NOW)
    await db_api.start_active(db, 42, sid1, ids["Math"], NOW)
    sid2 = await db_api.create_session(db, ids["Code"], NOW + 10)
    await db_api.start_active(db, 42, sid2, ids["Code"], NOW + 10)

    cursor = await db.execute("SELECT COUNT(*) AS n FROM active WHERE chat_id = 42")
    assert (await cursor.fetchone())["n"] == 1
    active = await db_api.get_active(db, 42)
    assert active["subject"] == "Code" and active["session_id"] == sid2
