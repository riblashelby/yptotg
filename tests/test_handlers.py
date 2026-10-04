"""Handler tests: real in-memory DB behind a mock bot, fake aiogram updates in."""
from __future__ import annotations

import time

import pytest

import database as db_api
import handlers
from tests.conftest import ADMIN_ID, OTHER_ID, make_callback, make_message


# --------------------------------------------------------------------------- /start
async def test_start_shows_subject_keyboard(live_bot):
    message = make_message(live_bot)
    await handlers.cmd_start(message)

    (_, text), kwargs = live_bot.calls[0][1], live_bot.calls[0][2]
    assert "Study Timer" in text
    buttons = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "Math" in buttons and "Code" in buttons


async def test_start_resumes_existing_session(live_bot):
    sid = await db_api.create_session(live_bot.db, "Math", time.time() - 120)
    await db_api.start_active(live_bot.db, ADMIN_ID, sid, "Math", time.time() - 120)

    await handlers.cmd_start(make_message(live_bot))

    (text,), _ = live_bot.calls[0][1], live_bot.calls[0][2]
    assert "Focusing" in text and "Math" in text


# --------------------------------------------------------------------------- start callback
async def test_cb_start_creates_session_and_active_row(live_bot):
    callback = make_callback(live_bot, "start:Math")
    await handlers.cb_start(callback)

    active = await db_api.get_active(live_bot.db, ADMIN_ID)
    assert active is not None and active["subject"] == "Math"

    totals_now = await db_api.get_day_totals(live_bot.db, __import__("datetime").date.today())
    assert totals_now == {}  # session still open — nothing logged yet

    (text,), kwargs = live_bot.calls[0][1], live_bot.calls[0][2]
    assert "Focusing" in text and "Math" in text
    assert kwargs["reply_markup"] is handlers.CONTROL_KEYBOARD
    assert live_bot.calls[-1][0] == "callback_answer"


async def test_cb_start_switches_subject_and_keeps_old_time(live_bot):
    now = time.time()
    old_sid = await db_api.create_session(live_bot.db, "Math", now - 600)
    await db_api.start_active(live_bot.db, ADMIN_ID, old_sid, "Math", now - 600)

    await handlers.cb_start(make_callback(live_bot, "start:Code"))

    active = await db_api.get_active(live_bot.db, ADMIN_ID)
    assert active["subject"] == "Code"

    totals = await db_api.get_day_totals(live_bot.db, __import__("datetime").date.today())
    assert totals["Math"] == pytest.approx(600.0, abs=2)  # old session banked


# --------------------------------------------------------------------------- pause
async def test_pause_then_resume_accumulates_only_paused_time(live_bot, monkeypatch):
    now = time.time()
    sid = await db_api.create_session(live_bot.db, "Math", now - 1000)
    await db_api.start_active(live_bot.db, ADMIN_ID, sid, "Math", now - 1000)

    clock = {"t": now}
    monkeypatch.setattr(time, "time", lambda: clock["t"])

    await handlers.cb_pause(make_callback(live_bot, "pause"))          # pause at T+0
    clock["t"] += 300                                                   # sit paused 5 min
    await handlers.cb_pause(make_callback(live_bot, "pause"))          # resume

    active = await db_api.get_active(live_bot.db, ADMIN_ID)
    assert active["paused_at"] is None
    assert active["paused_sec"] == pytest.approx(300.0)
    texts = [args[0] for name, args, _ in live_bot.calls if name == "edit_text"]
    assert any("Paused" in t for t in texts) and any("Focusing" in t for t in texts)


async def test_pause_without_session_alerts(live_bot):
    callback = make_callback(live_bot, "pause")
    await handlers.cb_pause(callback)

    (_, kwargs) = live_bot.calls[-1][1], live_bot.calls[-1][2]
    assert kwargs.get("show_alert") is True
    assert not await db_api.get_active(live_bot.db, ADMIN_ID)


# --------------------------------------------------------------------------- stop
async def test_stop_logs_net_seconds_and_clears_active(live_bot):
    now = time.time()
    sid = await db_api.create_session(live_bot.db, "Reading", now - 1000)
    await db_api.start_active(live_bot.db, ADMIN_ID, sid, "Reading", now - 1000)
    await db_api.set_paused(live_bot.db, ADMIN_ID, None, 400)  # 400s accrued pause

    await handlers.cb_stop(make_callback(live_bot, "stop"))

    assert await db_api.get_active(live_bot.db, ADMIN_ID) is None
    totals = await db_api.get_day_totals(live_bot.db, __import__("datetime").date.today())
    assert totals["Reading"] == pytest.approx(600.0, abs=2)

    (text,), _ = live_bot.calls[0][1], live_bot.calls[0][2]
    assert "Session complete" in text and "Reading" in text


async def test_stop_without_session_is_safe(live_bot):
    callback = make_callback(live_bot, "stop")
    await handlers.cb_stop(callback)
    assert live_bot.calls[-1][2].get("show_alert") is True


# --------------------------------------------------------------------------- fallback
async def test_plain_text_gets_nudge(live_bot):
    await handlers.cmd_fallback(make_message(live_bot, text="hello"))
    (text,), _ = live_bot.calls[0][1], live_bot.calls[0][2]
    assert "/start" in text
