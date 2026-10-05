"""Inline-first Telegram handlers. All state lives in SQLite — nothing in memory."""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database as db_api
import logic

router = Router()


# ---------------------------------------------------------------------------
# Keyboards (built from the live subjects table — add/rename reflects instantly)
# ---------------------------------------------------------------------------
def subject_keyboard(subjects: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    by_id = dict(subjects)
    rows = [
        [InlineKeyboardButton(text=by_id[sid], callback_data=f"start:{sid}") for sid in group]
        for group in logic.keyboard_grid(list(by_id), per_row=2)
    ]
    rows.append([InlineKeyboardButton(text="🏷 Manage subjects", callback_data="subjects")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


MANAGE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add subject", callback_data="subject:add")],
        [InlineKeyboardButton(text="✏️ Rename subject", callback_data="subject:rename")],
        [InlineKeyboardButton(text="🎓 Study now", callback_data="subjects:back")],
    ]
)


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


async def _subjects_markup(message_or_callback: Message | CallbackQuery) -> InlineKeyboardMarkup:
    conn = message_or_callback.bot.db
    return subject_keyboard(await db_api.list_subjects(conn))


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
        reply_markup=await _subjects_markup(message),
    )


@router.message(Command("subjects"))
async def cmd_subjects(message: Message) -> None:
    await message.answer("🏷 <b>Subjects</b>", reply_markup=MANAGE_KEYBOARD)


@router.message(Command("addsubject"))
async def cmd_add_subject(message: Message) -> None:
    name = (message.text or "").partition(" ")[2].strip()
    if not name:
        await message.answer("Usage: /addsubject <New Subject>")
        return
    try:
        subject_id, clean = await db_api.add_subject(message.bot.db, name)
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await message.answer(f"✅ Added <b>{clean}</b> (#{subject_id}). It's on your keyboard now 🎓")


@router.message(Command("renamesubject"))
async def cmd_rename_subject(message: Message) -> None:
    arg = (message.text or "").partition(" ")[2].strip()
    subjects = await db_api.list_subjects(message.bot.db)
    raw_id, payload = logic.rename_prompt(arg, subjects)
    if not raw_id:
        await message.answer(payload)
        return
    try:
        _, clean = await db_api.rename_subject(message.bot.db, int(raw_id), payload)
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await message.answer(f"✅ Renamed to <b>{clean}</b> — history kept 📊")


@router.message(F.text, ~F.text.startswith("/"))
async def cmd_fallback(message: Message) -> None:
    await message.answer("Use /start to control your focus session 🎓")


# ---------------------------------------------------------------------------
# Session callbacks
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("start:"))
async def cb_start(callback: CallbackQuery) -> None:
    raw_id = callback.data.split(":", 1)[1]
    subject = await db_api.get_subject(callback.bot.db, int(raw_id))
    if subject is None:
        await callback.answer("Subject was removed — pick another 🙃", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=await _subjects_markup(callback))
        return
    subject_id, name = subject

    now = time.time()
    conn = callback.bot.db
    existing = await db_api.get_active(conn, callback.from_user.id)
    if existing:  # switch subject without losing logged time
        accrued = existing["paused_sec"] + (
            now - existing["paused_at"] if existing["paused_at"] else 0.0
        )
        await db_api.finish_session(conn, existing["session_id"], now, accrued)
        await db_api.clear_active(conn, callback.from_user.id)

    session_id = await db_api.create_session(conn, subject_id, now)
    await db_api.start_active(conn, callback.from_user.id, session_id, subject_id, now)
    await callback.message.edit_text(
        logic.render_status(name, 0.0, paused=False), reply_markup=CONTROL_KEYBOARD
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


# ---------------------------------------------------------------------------
# Subject-management callbacks
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "subjects")
async def cb_subjects(callback: CallbackQuery) -> None:
    await callback.message.edit_text("🏷 <b>Manage subjects</b>", reply_markup=MANAGE_KEYBOARD)
    await callback.answer()


@router.callback_query(F.data == "subjects:back")
async def cb_subjects_back(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🎓 <b>Study Timer</b>\n\nPick a subject to start focusing 👇",
        reply_markup=await _subjects_markup(callback),
    )
    await callback.answer()


@router.callback_query(F.data == "subject:add")
async def cb_subject_add(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "➕ Send me the new subject name as a command:\n\n"
        "<code>/addsubject Physics</code>"
    )
    await callback.answer()


@router.callback_query(F.data == "subject:rename")
async def cb_subject_rename(callback: CallbackQuery) -> None:
    subjects = await db_api.list_subjects(callback.bot.db)
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"renamepick:{sid}")]
        for sid, name in subjects
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="subjects")])
    await callback.message.edit_text(
        "✏️ Pick the subject to rename:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("renamepick:"))
async def cb_rename_pick(callback: CallbackQuery) -> None:
    subject_id = int(callback.data.split(":", 1)[1])
    subject = await db_api.get_subject(callback.bot.db, subject_id)
    if subject is None:
        await callback.answer("Subject vanished 🤷", show_alert=True)
        return
    await callback.message.edit_text(
        f"✏️ Renaming <b>{subject[1]}</b> (#{subject_id}).\n\n"
        "Send:\n"
        f"<code>/renamesubject {subject_id} New Name</code>"
    )
    await callback.answer()
