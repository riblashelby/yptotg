"""Shared fixtures: in-memory DB, mock bot, and aiogram-model factories."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from pydantic import ConfigDict, PrivateAttr

from aiogram.types import CallbackQuery, Chat, Message, User

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db_api  # noqa: E402

ADMIN_ID = 111111111
OTHER_ID = 999999999
CHANNEL_ID = -1001111111111


class MockBot:
    """Stands in for aiogram Bot: records method calls, never touches the network."""

    model = "mock"  # aiogram's CallbackQuery.answer() accesses bot.session.bot.model

    def __init__(self, conn: Any) -> None:
        self.db = conn
        self.recorder = Recorder()
        self.session = SimpleNamespace(bot=self)  # models resolve `obj.bot` via context

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


class TelegramTestMixin:
    """aiogram reads the bot off the private `_bot` attr — wire it for frozen models."""

    def bind_bot(self, bot: MockBot) -> "TelegramTestMixin":
        object.__setattr__(self, "_bot", bot)  # frozen-safe assignment
        return self


class RecordingMessage(TelegramTestMixin, Message):
    """aiogram models are frozen pydantic classes — declare recorder as PrivateAttr."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    _recorder: Recorder = PrivateAttr(default_factory=Recorder)

    def bind_recorder(self, recorder: Recorder) -> "RecordingMessage":
        self._recorder = recorder
        return self

    async def answer(self, text: str, **kwargs: Any) -> "RecordingMessage":
        self._recorder.record("answer", text, **kwargs)
        return self

    async def edit_text(self, text: str, **kwargs: Any) -> "RecordingMessage":
        self._recorder.record("edit_text", text, **kwargs)
        return self

    async def edit_reply_markup(self, **kwargs: Any) -> "RecordingMessage":
        self._recorder.record("edit_reply_markup", **kwargs)
        return self


class RecordingCallback(TelegramTestMixin, CallbackQuery):
    """CallbackQuery whose answer responses are recorded."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    _recorder: Recorder = PrivateAttr(default_factory=Recorder)

    def bind_recorder(self, recorder: Recorder) -> "RecordingCallback":
        self._recorder = recorder
        return self

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs: Any) -> bool:
        self._recorder.record("callback_answer", text, show_alert=show_alert, **kwargs)
        return True


def make_message(bot: MockBot, user_id: int = ADMIN_ID, text: str = "/start") -> RecordingMessage:
    # aiogram 3.31+ resolves `obj.bot` from model *context* — the plain `bot=`
    # kwarg is silently ignored, so bind explicitly with `.as_(bot)`.
    return RecordingMessage(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=make_user(user_id),
        text=text,
    ).as_(bot).bind_recorder(bot.recorder)  # type: ignore[arg-type]


def make_callback(bot: MockBot, data: str, user_id: int = ADMIN_ID) -> RecordingCallback:
    return RecordingCallback(
        id="callback-id",
        from_user=make_user(user_id),
        chat_instance="instance",
        data=data,
        message=make_message(bot, user_id=user_id),
    ).as_(bot).bind_recorder(bot.recorder)  # type: ignore[arg-type]
