"""Entry point: bot/dispatcher wiring, admin gate, and a zero-dependency daily loop.

Run with:  python bot.py   (configuration comes from .env only)
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import ssl
from datetime import date, datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp_socks import ProxyConnector

import database as db_api
import logic
from config import Settings
from handlers import router
from middleware import AdminOnlyMiddleware

log = logging.getLogger("study-bot")


def _socks_connector_init(proxy_url: str) -> dict[str, Any]:
    """Build ProxyConnector kwargs the way aiogram itself does internally.

    ``ProxyConnector`` has NO ``url=`` parameter — it needs
    ``proxy_type/host/port/username/password/rdns``. Handing it ``{"url": ...}``
    is what produced ``missing 2 required positional arguments: 'host' and 'port'``.
    """
    from aiohttp_socks.utils import parse_proxy_url

    proxy_type, host, port, username, password = parse_proxy_url(proxy_url)
    try:  # literal IP → resolve locally (rdns=False); hostname → resolve at the proxy (rdns=True)
        ipaddress.ip_address(host)
        rdns = False
    except ValueError:
        rdns = True
    return {
        "proxy_type": proxy_type,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "rdns": rdns,
    }


class ProxiedSession(AiohttpSession):
    """AiohttpSession forced onto a SOCKS5/HTTP proxy connector.

    Two forbidden approaches that killed earlier deployments:
      * ``connector=`` as a constructor kwarg → ``BaseSession.__init__() got
        an unexpected keyword argument 'connector'``;
      * ``_connector_init = {"url": ...}`` → ``ProxyConnector.__init__()
        missing 'host' and 'port'``.
    The supported extension point: rewrite ``_connector_type/_connector_init``
    *after* construction with properly parsed kwargs — version-proof on every
    aiogram 3.x build.
    """

    def __init__(self, proxy_url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        from aiohttp_socks import ProxyConnector

        self._connector_type = ProxyConnector
        self._connector_init = _socks_connector_init(proxy_url)
        self._should_reset_connector = True


def build_session(proxy_url: str | None) -> BaseSession:
    """Session factory.

    Direct mode returns a plain ``AiohttpSession`` — aiogram 3.31 already pins
    the certifi CA bundle itself, so TLS works on slim images out of the box.
    Proxy mode returns :class:`ProxiedSession` (connector rewritten post-init).
    """
    return ProxiedSession(proxy_url) if proxy_url else AiohttpSession()


def parse_proxy_host(proxy_url: str) -> tuple[str, int]:
    """Extract (host, port) from a proxy URL like socks5://[user:pass@]host:1080."""
    tail = proxy_url.rsplit("@", 1)[-1].rsplit("://", 1)[-1]
    host, _, port = tail.partition(":")
    return host, int(port) if port else 1080


def proxy_hint(host: str, port: int) -> str:
    return (
        f"  1) docker ps -a | grep xray-proxy          # proxy container running?\n"
        f"  2) docker network connect study-bot-net xray-proxy   # DNS broke after 'down'\n"
        f"  3) test it yourself:\n"
        f"       docker exec study-timer python -c \"import socket;"
        f"socket.create_connection(('{host}',{port}),5);print('PROXY OK')\"\n"
        f"  4) go direct (if your server reaches Telegram without a proxy):"
        f" set PROXY_URL= in .env and restart"
    )


async def preflight(session: BaseSession, proxy_url: str | None) -> bool:
    """TCP probe of the configured proxy. Fail-fast: exit(1) with actionable hints.

    A dead proxy otherwise turns every API call into a silent hang/timeout loop;
    crashing immediately (and letting the restart policy retry) surfaces the real
    problem in the logs instead.
    """
    if not proxy_url:
        return True
    host, port = parse_proxy_host(proxy_url)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        raise SystemExit(f"Proxy {host}:{port} unreachable ({exc!r}) — aborting.\n{proxy_hint(host, port)}")
    writer.close()
    await writer.wait_closed()
    log.info("Proxy preflight OK: %s:%s", host, port)
    return True


def build_bot(settings: Settings) -> Bot:
    """Bot factory — proxy wired only when ``PROXY_URL`` is set."""
    kwargs: dict[str, Any] = {"default": DefaultBotProperties(parse_mode=ParseMode.HTML)}
    if settings.proxy_url:
        log.info("Using proxy: %s", settings.proxy_url.split("@")[-1])
        kwargs["session"] = build_session(settings.proxy_url)
    else:
        kwargs["session"] = build_session(None)
    return Bot(token=settings.bot_token, **kwargs)


async def send_daily_report(bot: Any, db: Any, channel_id: int | str, day: date) -> str:
    """Render today's totals and push them to the channel. Returns the report text."""
    totals = await db_api.get_day_totals(db, day)
    report = logic.render_daily_report(totals, day)
    await bot.send_message(channel_id, report)
    return report


async def daily_report_loop(bot: Any, db: Any, settings: Settings) -> None:
    """Native asyncio scheduler: sleep until REPORT_TIME, fire, repeat. No cron needed."""
    while True:
        now = datetime.now()
        sleep_for = ((settings.report_minutes + 1 - (now.hour * 60 + now.minute)) % 1440) * 60
        await asyncio.sleep(sleep_for or 60)
        try:
            await send_daily_report(bot, db, settings.report_channel_id, date.today())
            log.info("Daily report delivered ✅")
        except Exception:  # keep the loop alive no matter what
            log.exception("Daily report failed")
        await asyncio.sleep(61)  # drift guard: never double-fire in the same minute


async def resilient_startup(bot: Bot, settings: Settings) -> None:
    """Retry the cosmetic startup calls until they succeed — never crash.

    aiogram's polling loop reconnects on its own; these calls only make the bot
    look polished (menu commands). ``delete_webhook`` is folded in here so a
    transient proxy/API hiccup can't kill startup outright.
    """
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Start focusing"),
                    BotCommand(command="subjects", description="Add / rename subjects"),
                    BotCommand(command="addsubject", description="Add a subject: /addsubject Physics"),
                    BotCommand(command="renamesubject", description="Rename: /renamesubject <id> <New Name>"),
                ],
                request_timeout=30,
            )
            log.info("Telegram API reachable ✅ · menu commands set")
            return
        except Exception as exc:  # keep trying; polling self-heals regardless
            log.warning("Startup API check failed, retrying in 15s: %s", exc)
            await asyncio.sleep(15)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()

    # Belt & braces: drop generic proxy vars that confuse some HTTP stacks.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(var, None)

    bot = build_bot(settings)
    await preflight(bot.session, settings.proxy_url)  # fail-fast with hints if proxy is dead

    db = await db_api.init_db(settings.db_path)
    bot.db = db  # single-connection app; handlers read it off the bot for easy mocking

    dp = Dispatcher()
    dp.update.outer_middleware(AdminOnlyMiddleware(settings.admin_id))
    dp.include_router(router)

    log.info("Study bot online for admin %s", settings.admin_id)

    report_task = asyncio.create_task(daily_report_loop(bot, db, settings))
    startup_task = asyncio.create_task(resilient_startup(bot, settings))
    try:
        await dp.start_polling(bot)  # retries the network forever on its own
    finally:
        startup_task.cancel()
        report_task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bye 👋")
