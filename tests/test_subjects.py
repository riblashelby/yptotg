"""Subject management: DB registry, commands, inline flows, and v1→v2 migration."""
from __future__ import annotations

import time
from datetime import date

import aiosqlite
import pytest

import database as db_api
import handlers
import logic
from tests.conftest import ADMIN_ID, make_callback, make_message


# --------------------------------------------------------------------------- commands
async def test_addsubject_command_creates_registry_row(live_bot):
    await handlers.cmd_add_subject(make_message(live_bot, text="/addsubject Physics"))

    assert await db_api.find_subject(live_bot.db, "Physics") is not None
    (text,), _ = live_bot.calls[0][1], live_bot.calls[0][2]
    assert "Added" in text and "Physics" in text


async def test_addsubject_usage_and_duplicate(live_bot):
    await handlers.cmd_add_subject(make_message(live_bot, text="/addsubject"))
    assert "Usage" in live_bot.calls[0][1][0]

    await handlers.cmd_add_subject(make_message(live_bot, text="/addsubject Math"))
    assert "⚠️" in live_bot.calls[-1][1][0]  # duplicate → ValueError surfaced


async def test_renamesubject_command_updates_history_label(live_bot):
    now = time.time()
    code_id, _ = await db_api.find_subject(live_bot.db, "Code")
    sid = await db_api.create_session(live_bot.db, code_id, now - 600)
    await db_api.finish_session(live_bot.db, sid, now)

    await handlers.cmd_rename_subject(make_message(live_bot, text=f"/renamesubject {code_id} TypeScript"))

    assert await db_api.get_subject(live_bot.db, code_id) == (code_id, "TypeScript")
    totals = await db_api.get_day_totals(live_bot.db, date.today())
    assert "TypeScript" in totals  # old stats follow the new label
    assert "Renamed" in live_bot.calls[0][1][0]


async def test_renamesubject_bad_id_lists_choices(live_bot):
    await handlers.cmd_rename_subject(make_message(live_bot, text="/renamesubject 99999 Nope"))
    assert "No subject with id" in live_bot.calls[0][1][0]

    await handlers.cmd_rename_subject(make_message(live_bot, text="/renamesubject"))
    assert "Usage" in live_bot.calls[-1][1][0]


# --------------------------------------------------------------------------- inline flow
async def test_manage_keyboard_offers_add_and_rename(live_bot):
    callback = make_callback(live_bot, "subjects")
    await handlers.cb_subjects(callback)
    buttons = [b.text for row in live_bot.recorder.calls[-1][2]["reply_markup"].inline_keyboard for b in row]
    assert {"➕ Add subject", "✏️ Rename subject"} <= set(buttons)


async def test_main_menu_lists_all_subjects_plus_manage(live_bot):
    """The /start grid: every registry subject + the management entry point."""
    await handlers.cmd_start(make_message(live_bot, text="/start"))
    buttons = [b.text for row in live_bot.calls[0][2]["reply_markup"].inline_keyboard for b in row]
    assert {"Math", "Code", "Reading", "🏷 Manage subjects"} <= set(buttons)


async def test_rename_picker_shows_current_subjects(live_bot):
    await handlers.cb_subject_rename(make_callback(live_bot, "subject:rename"))
    rows = live_bot.recorder.calls[-1][2]["reply_markup"].inline_keyboard
    labels = [b.text for row in rows for b in row]
    assert "Math" in labels and "⬅️ Back" in labels
    assert rows[0][0].callback_data.startswith("renamepick:")


async def test_rename_pick_shows_ready_command(live_bot):
    math_id = (await db_api.find_subject(live_bot.db, "Math"))[0]
    await handlers.cb_rename_pick(make_callback(live_bot, f"renamepick:{math_id}"))
    assert f"/renamesubject {math_id} New Name" in live_bot.recorder.calls[-1][1][0]


async def test_start_keyboard_includes_newly_added_subject(live_bot):
    await db_api.add_subject(live_bot.db, "Astronomy")
    message = make_message(live_bot)
    await handlers.cmd_start(message)
    buttons = [b.text for row in message._recorder.calls[-1][2]["reply_markup"].inline_keyboard for b in row]
    assert "Astronomy" in buttons and "🏷 Manage subjects" in buttons


async def test_start_with_stale_subject_id_is_handled(live_bot):
    """Keyboard pressed after the subject vanished elsewhere → graceful refresh."""
    callback = make_callback(live_bot, "start:424242")
    await handlers.cb_start(callback)
    assert "removed" in live_bot.recorder.calls[-1][1][0]
    kinds = [name for name, _, _ in live_bot.recorder.calls]
    assert "edit_reply_markup" in kinds


# --------------------------------------------------------------------------- v1 → v2 migration
_V1_SCHEMA = """
CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT    NOT NULL,
    start_ts   REAL    NOT NULL,
    end_ts     REAL,
    paused_sec REAL    NOT NULL DEFAULT 0
);
CREATE TABLE active (
    chat_id    INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    subject    TEXT    NOT NULL,
    started_at REAL    NOT NULL,
    paused_sec REAL    NOT NULL DEFAULT 0,
    paused_at  REAL
);
"""


@pytest.fixture
async def legacy_db():
    """A database frozen at the old text-subject schema, with real data."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_V1_SCHEMA)
    now = time.time()
    await conn.execute(
        "INSERT INTO sessions (subject, start_ts, end_ts, paused_sec) VALUES (?, ?, ?, ?)",
        ("Korean", now - 900, now - 300, 120),
    )
    await conn.execute(
        "INSERT INTO sessions (subject, start_ts, end_ts, paused_sec) VALUES (?, ?, ?, ?)",
        ("Math", now - 86_400, now - 82_800, 0),
    )
    sid = (await (await conn.execute("SELECT id FROM sessions WHERE subject = 'Korean'")).fetchone())["id"]
    await conn.execute(
        "INSERT INTO active (chat_id, session_id, subject, started_at, paused_sec, paused_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ADMIN_ID, sid, "Physics", now - 60, 0, None),
    )
    await conn.commit()
    yield conn
    await conn.close()


async def test_migrate_preserves_history_and_live_timer(legacy_db):
    from migrations import migrate

    await migrate(legacy_db)
    await migrate(legacy_db)  # idempotent — second run is a no-op

    names = {name for _, name in await db_api.list_subjects(legacy_db)}
    assert {"Korean", "Physics", "Math"} <= set(names)          # history + live name kept

    cursor = await legacy_db.execute(
        "SELECT s.name AS n, t.end_ts - t.start_ts - t.paused_sec AS secs FROM sessions t JOIN subjects s ON s.id=t.subject_id"
    )
    rows = {row["n"]: row["secs"] for row in await cursor.fetchall()}
    assert rows["Korean"] == pytest.approx(480.0)               # 900-600-120? -> 600-120 = 480 net
    assert "Math" in rows

    active = await db_api.get_active(legacy_db, ADMIN_ID)
    assert active is not None and active["subject"] == "Physics"
    assert active["session_id"] > 0

    cursor = await legacy_db.execute("PRAGMA user_version")
    assert (await cursor.fetchone())[0] == 2
