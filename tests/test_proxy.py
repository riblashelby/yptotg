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


def test_build_session_without_proxy_is_plain_aiohttp() -> None:
    """No PROXY_URL → direct AiohttpSession with a hardened (certifi) SSL context."""
    session = bot_module.build_session(None)
    from aiogram.client.session.aiohttp import AiohttpSession
    assert isinstance(session, AiohttpSession)
    assert not isinstance(session, bot_module.ProxiedSession)
    assert session.ssl_context is not None


def test_preflight_fails_fast_on_unreachable_proxy() -> None:
    """Unresolvable proxy host → SystemExit with actionable hint, never a hang."""
    import asyncio

    async def boom() -> None:
        session = bot_module.build_session("socks5://no-such-host.invalid:1080")
        await bot_module.preflight(session, "socks5://no-such-host.invalid:1080")

    with pytest.raises(SystemExit) as exc:
        asyncio.run(boom())
    assert "unreachable" in str(exc.value)


def test_preflight_skipped_without_proxy() -> None:
    import asyncio
    asyncio.run(bot_module.preflight(bot_module.build_session(None), None))
