"""Shared fixtures: in-memory DB, mock bot, and aiogram-model factories."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from pydantic import ConfigDict

from aiogram.types import CallbackQuery, Chat, Message, User

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db_api  # noqa: E402

ADMIN_ID = 111111111
OTHER_ID = 999999999
CHANNEL_ID = -1001111111111


class MockBot:
    """Stands in for aiogram Bot: records method calls, never touches the network."""

    def __init__(self, conn: Any) -> None:
        self.db = conn
        self.recorder = Recorder()

    async def send_message(self, chat_id: Any, text: str, **kwargs: Any) -> None:
        self.recorder.record("send_message", chat_id, text, **kwargs)

    @property
    def calls(self) -> list[tuple[str, tuple, dict]]:
        return self.recorder.calls

    @property
    def sent(self) -> list[tuple[Any, str]]:
        return [args[:2] for name, args, _ in self.calls if name == "send_message"]


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    """Fresh in-memory SQLite per test."""
    conn = await db_api.init_db(":memory:")
    yield conn
    await conn.close()


@pytest.fixture
def mock_bot() -> MockBot:
    """Bot mock with no DB attached — for middleware/report tests."""
    return MockBot(conn=None)


@pytest.fixture
def live_bot(db: Any) -> MockBot:
    """Bot mock carrying the in-memory DB connection as `.db` (like bot.py does)."""
    return MockBot(conn=db)


def make_user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Tester")


class Recorder:
    """Collects every outgoing API call so tests can assert on them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def record(self, kind: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((kind, args, kwargs))


class RecordingMessage(Message):
    """aiogram models are frozen pydantic classes — subclass to override methods."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    recorder: Recorder = Recorder()

    async def answer(self, text: str, **kwargs: Any) -> "RecordingMessage":
        self.recorder.record("answer", text, **kwargs)
        return self


class RecordingCallback(CallbackQuery):
    """CallbackQuery whose answer/edit responses are recorded."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    recorder: Recorder = Recorder()

    async def answer(self, text: str = "", **kwargs: Any) -> bool:
        self.recorder.record("callback_answer", text, **kwargs)
        return True


def make_message(bot: MockBot, user_id: int = ADMIN_ID, text: str = "/start") -> RecordingMessage:
    message = RecordingMessage(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=make_user(user_id),
        text=text,
        bot=bot,  # type: ignore[arg-type]
        recorder=bot.recorder,
    )
    return message


def make_callback(bot: MockBot, data: str, user_id: int = ADMIN_ID) -> RecordingCallback:
    message = make_message(bot, user_id=user_id)

    async def edit_text(text: str, **kwargs: Any) -> RecordingMessage:
        bot.recorder.record("edit_text", text, **kwargs)
        return message

    message.edit_text = edit_text  # allowed: subclass instances keep a mutable __dict__
    return RecordingCallback(
        id="callback-id",
        from_user=make_user(user_id),
        chat_instance="instance",
        data=data,
        message=message,
        bot=bot,  # type: ignore[arg-type]
        recorder=bot.recorder,
    )
