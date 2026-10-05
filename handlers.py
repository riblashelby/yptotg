"""Inline-first Telegram handlers. All state lives in SQLite — nothing in memory."""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database as db_api
import logic
from config import DEFAULT_TIMER_CONFIG, TimerConfig

router = Router()


def subject_keyboard(timer_config: TimerConfig = DEFAULT_TIMER_CONFIG) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"start:{name}") for name in group]
        for group in zip(timer_config.subjects[::2], timer_config.subjects[1::2], strict=False)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


CONTROL_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="⏸ Pause", callback_data="pause"),
            InlineKeyboardButton(text="⏹ Stop", callback_data="stop"),
        ]
    ]
)


def _timer_text(active: dict, now: float) -> str:
    paused = active["paused_at"] is not None
    seconds = logic.elapsed_seconds(active["started_at"], now, active["paused_sec"], active["paused_at"])
    return logic.render_status(active["subject"], seconds, paused)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    active = await db_api.get_active(message.bot.db, message.chat.id)
    if active:
        await message.answer(_timer_text(active, time.time()), reply_markup=CONTROL_KEYBOARD)
        return
    await message.answer(
        "🎓 <b>Study Timer</b>\n\nPick a subject to start focusing 👇",
        reply_markup=subject_keyboard(),
    )


@router.message(F.text, ~F.text.startswith("/"))
async def cmd_fallback(message: Message) -> None:
    await message.answer("Use /start to control your focus session 🎓")


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("start:"))
async def cb_start(callback: CallbackQuery) -> None:
    subject = callback.data.split(":", 1)[1]
    now = time.time()
    conn = callback.bot.db
    existing = await db_api.get_active(conn, callback.from_user.id)
    if existing:  # switch subject without losing logged time
        await db_api.finish_session(conn, existing["session_id"], now)
        await db_api.clear_active(conn, callback.from_user.id)

    session_id = await db_api.create_session(conn, subject, now)
    await db_api.start_active(conn, callback.from_user.id, session_id, subject, now)
    await callback.message.edit_text(
        logic.render_status(subject, 0.0, paused=False), reply_markup=CONTROL_KEYBOARD
    )
    await callback.answer()


@router.callback_query(F.data == "pause")
async def cb_pause(callback: CallbackQuery) -> None:
    conn = callback.bot.db
    active = await db_api.get_active(conn, callback.from_user.id)
    if not active:
        await callback.answer("No active session 🤷", show_alert=True)
        return
    now = time.time()
    paused_at, accrued = logic.new_paused_state(active["paused_at"], now, active["paused_sec"])
    await db_api.set_paused(conn, callback.from_user.id, paused_at, accrued)
    action = "resumed" if paused_at is None else "paused"
    await callback.message.edit_text(_timer_text({**active, "paused_at": paused_at, "paused_sec": accrued}, now))
    await callback.answer(f"Session {action} ✨")


@router.callback_query(F.data == "stop")
async def cb_stop(callback: CallbackQuery) -> None:
    conn = callback.bot.db
    active = await db_api.get_active(conn, callback.from_user.id)
    if not active:
        await callback.answer("No active session 🤷", show_alert=True)
        return
    now = time.time()
    seconds = logic.elapsed_seconds(active["started_at"], now, active["paused_sec"], active["paused_at"])
    # Persist the accrued pause too, otherwise paused time inflates the stored total.
    accrued = active["paused_sec"] + (now - active["paused_at"] if active["paused_at"] else 0.0)
    await db_api.finish_session(conn, active["session_id"], now, accrued)
    await db_api.clear_active(conn, callback.from_user.id)
    await callback.message.edit_text(
        f"✅ <b>Session complete!</b>\n\n"
        f"📚 {active['subject']} · <b>{logic.format_duration(seconds)}</b> logged\n\n"
        f"New round? /start 🚀"
    )
    await callback.answer()
