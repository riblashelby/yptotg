"""Regression tests for SOCKS5 wiring — the `connector=` TypeError crash class."""
from __future__ import annotations

import asyncio

import pytest

import bot as bot_module
from config import Settings

PROXY = "socks5://xray-proxy:1080"


def _settings(**overrides: object) -> Settings:
    base = dict(bot_token="42:TEST", admin_id=1, report_channel_id=-100, **overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_build_session_uses_connector_override() -> None:
    """Session must inject the SOCKS connector via create_connector(), never a kwarg."""
    from aiohttp_socks import ProxyConnector

    session = bot_module.build_session(PROXY)
    assert isinstance(session, bot_module.ProxiedSession)
    connector = asyncio.run(session.create_connector())
    assert isinstance(connector, ProxyConnector)


def test_build_bot_with_proxy_attaches_proxied_session() -> None:
    b = bot_module.build_bot(_settings(proxy_url=PROXY))
    assert isinstance(b.session, bot_module.ProxiedSession)


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
    assert not isinstance(b.session, bot_module.ProxiedSession)
