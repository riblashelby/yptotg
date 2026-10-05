"""Regression tests for SOCKS5 wiring — the `connector=` TypeError crash class."""
from __future__ import annotations

import pytest

import bot as bot_module
from config import Settings

PROXY = "socks5://xray-proxy:1080"


def _settings(**overrides: object) -> Settings:
    base = dict(bot_token="42:TEST", admin_id=1, report_channel_id=-100, **overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_build_session_uses_native_proxy_kwarg() -> None:
    """aiogram >=3.15 accepts `proxy=`; we must use it, never `connector=`."""
    session = bot_module.build_session(PROXY)
    assert type(session).__name__ == "AiohttpSession"


def test_build_session_never_passes_connector_kwarg() -> None:
    """The old code crashed here: BaseSession.__init__() got 'connector'."""
    from aiogram.client.session.aiohttp import AiohttpSession

    with pytest.raises(TypeError):
        AiohttpSession(connector=object())  # documented broken path

    bot_module.build_session(PROXY)  # ours does not raise


def test_build_bot_without_proxy_has_default_session() -> None:
    from aiogram.client.session.aiohttp import AiohttpSession

    b = bot_module.build_bot(_settings(proxy_url=None))
    assert isinstance(b.session, AiohttpSession)


def test_build_bot_with_proxy_attaches_session() -> None:
    b = bot_module.build_bot(_settings(proxy_url=PROXY))
    assert type(b.session).__name__ == "AiohttpSession"
