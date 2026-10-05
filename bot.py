"""Entry point: bot/dispatcher wiring, admin gate, and a zero-dependency daily loop.

Run with:  python bot.py   (configuration comes from .env only)
"""
from __future__ import annotations

import asyncio
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


class ProxiedSession(AiohttpSession):
    """AiohttpSession with a SOCKS5/HTTP connector injected the supported way.

    Passing ``connector=`` to the constructor is forbidden by aiogram
    (``BaseSession.__init__() got an unexpected keyword argument 'connector'``
    — the crash that killed earlier deployments). The supported extension
    point is overriding ``create_connector()``, which works on every
    aiogram 3.x build regardless of its native ``proxy=`` support.
    """

    def __init__(self, proxy_url: str, **kwargs: Any) -> None:
        self._proxy_url = proxy_url
        super().__init__(**kwargs)

    async def create_connector(self) -> ProxyConnector:  # type: ignore[override]
        return ProxyConnector.from_url(self._proxy_url)


def build_session(proxy_url: str | None) -> BaseSession:
    """Session factory: plain HTTPS (with hardened TLS) or SOCKS5 via override.

    NOTE: aiogram 3.31's ``AiohttpSession.__init__`` forwards **kwargs straight to
    ``BaseSession`` — passing ``ssl_context=`` there raises TypeError just like the
    old ``connector=`` crash. The safe path: construct plainly, then swap in a
    certifi-backed SSL context on the instance after construction.
    """
    if not proxy_url:
        session = AiohttpSession()
        try:
            import certifi
            session.ssl = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # pragma: no cover - certifi ships with aiohttp anyway
            pass
        return session
    return ProxiedSession(proxy_url)


def parse_proxy_host(proxy_url: str) -> tuple[str, int]:
    """Extract (host, port) from a proxy URL like socks5://[user:pass@]host:1080."""
    tail = proxy_url.rsplit("@", 1)[-1].rsplit("://", 1)[-1]
    host, _, port = tail.partition(":")
    return host, int(port) if port else 1080


async def preflight(session: BaseSession, proxy_url: str | None) -> bool:
    """Best-effort reachability probe. NEVER crashes startup — returns False instead.

    Polling retries connections on its own forever, so a failed probe only means
    "log a hint now"; the bot still starts and self-heals once the proxy is back.
    """
    if not proxy_url:
        return True
    host, port = parse_proxy_host(proxy_url)
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.getaddrinfo(host, port), timeout=5)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
        writer.close()
        await writer.wait_closed()
        log.info("Proxy preflight OK: %s:%s", host, port)
        return True
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        log.warning(
            "⚠️  Proxy %s:%s not reachable yet (%r). Starting anyway — polling will retry.\n"
            "    If this persists, re-attach the proxy container to the bot's network:\n"
            "    docker network connect study-bot-net xray-proxy",
            host, port, exc,
        )
        return False


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


async def startup_guard(bot: Bot, settings: Settings) -> bool:
    """Pre-flight API check. Non-fatal: logs a diagnosis and lets polling retry."""
    try:
        await bot.get_me()
        log.info("Telegram API reachable ✅")
        return True
    except Exception as exc:
        reason = str(exc).lower()
        if "certificate" in reason or "ssl" in reason:
            hint = "TLS/CA issue — try running WITHOUT proxy: PROXY_URL= in .env"
        elif "timeout" in reason or "connect" in reason:
            host, port = parse_proxy_host(settings.proxy_url or "")
            hint = (
                f"Network path to Telegram broken via {host}:{port}. Re-attach the proxy:\n"
                f"    docker network connect study-bot-net xray-proxy\n"
                f"  …or go direct: set PROXY_URL= (empty) in .env"
            )
        else:
            hint = "Will keep retrying via polling."
        log.warning("⚠️  Telegram API not reachable yet: %s. %s", exc, hint)
        return False


async def resilient_startup(bot: Bot, settings: Settings) -> None:
    """Retry the two cheap startup calls until they succeed — but NEVER crash.

    aiogram's polling loop reconnects on its own; these calls only make the bot
    look polished (menu commands) and confirm connectivity in the logs.
    """
    while True:
        ok_api = await startup_guard(bot, settings)
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Start focusing"),
                    BotCommand(command="subjects", description="Add / rename subjects"),
                    BotCommand(command="addsubject", description="Add a subject: /addsubject Physics"),
                    BotCommand(command="renamesubject", description="Rename: /renamesubject <id> <New Name>"),
                ],
                request_timeout=30,
            )
            ok_cmd = True
        except Exception as exc:
            log.warning("set_my_commands failed (ignored): %s", exc)
            ok_cmd = False
        if ok_api and ok_cmd:
            return
        await asyncio.sleep(15)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()

    # Belt & braces: drop generic proxy vars that confuse some HTTP stacks.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(var, None)

    bot = build_bot(settings)
    await preflight(bot.session, settings.proxy_url)  # advisory only — never raises

    db = await db_api.init_db(settings.db_path)
    bot.db = db  # single-connection app; handlers read it off the bot for easy mocking

    dp = Dispatcher()
    dp.update.outer_middleware(AdminOnlyMiddleware(settings.admin_id))
    dp.include_router(router)

    log.info("Study bot online for admin %s", settings.admin_id)

    report_task = asyncio.create_task(daily_report_loop(bot, db, settings))
    startup_task = asyncio.create_task(resilient_startup(bot, settings))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
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
