"""Strict admin-only gate: any update from a non-ADMIN_ID user is silently dropped."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class AdminOnlyMiddleware(BaseMiddleware):
    """One job, done well: only the owner talks to this bot."""

    def __init__(self, admin_id: int) -> None:
        self.admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self.admin_id:
            return None  # ignore strangers entirely — no reply, no logs, no bloat
        return await handler(event, data)
