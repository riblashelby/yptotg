"""Proof that the admin gate blocks strangers and lets the owner through."""
from __future__ import annotations

import pytest
from aiogram.types import Update

from middleware import AdminOnlyMiddleware
from tests.conftest import ADMIN_ID, OTHER_ID, make_message, make_user

pytestmark = pytest.mark.asyncio


async def _pass_handler(event, data):
    return "handled"


def _update_data(user_id: int | None) -> dict:
    user = make_user(user_id) if user_id is not None else None
    return {"event_from_user": user}


async def test_admin_passes_through(mock_bot):
    middleware = AdminOnlyMiddleware(admin_id=ADMIN_ID)
    event = make_message(mock_bot, user_id=ADMIN_ID)

    result = await middleware(_pass_handler, event, _update_data(ADMIN_ID))
    assert result == "handled"


async def test_stranger_is_silently_blocked(mock_bot):
    middleware = AdminOnlyMiddleware(admin_id=ADMIN_ID)
    event = make_message(mock_bot, user_id=OTHER_ID)

    result = await middleware(_pass_handler, event, _update_data(OTHER_ID))
    assert result is None            # handler never invoked
    assert mock_bot.calls == []      # and nobody gets a reply


async def test_anonymous_channel_post_blocked():
    """Updates without a `from_user` (e.g. channel posts) must not reach handlers."""
    middleware = AdminOnlyMiddleware(admin_id=ADMIN_ID)
    result = await middleware(_pass_handler, Update(update_id=1, **{}), _update_data(None))
    assert result is None
