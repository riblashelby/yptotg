"""Entry point: bot/dispatcher wiring, admin gate, and a zero-dependency daily loop.

Run with:  python bot.py   (configuration comes from .env only)
"""
from __future__ import annotations

import asyncio
import logging
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


def build_session(proxy_url: str) -> BaseSession:
    """SOCKS5/HTTP session factory — connector injected via create_connector()."""
    return ProxiedSession(proxy_url)


def build_bot(settings: Settings) -> Bot:
    """Bot factory — proxy wired only when ``PROXY_URL`` is set."""
    kwargs: dict[str, Any] = {"default": DefaultBotProperties(parse_mode=ParseMode.HTML)}
    if settings.proxy_url:
        log.info("Using proxy: %s", settings.proxy_url.split("@")[-1])
        kwargs["session"] = build_session(settings.proxy_url)
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


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()

    bot = build_bot(settings)
    db = await db_api.init_db(settings.db_path)
    bot.db = db  # single-connection app; handlers read it off the bot for easy mocking

    dp = Dispatcher()
    dp.update.outer_middleware(AdminOnlyMiddleware(settings.admin_id))
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Start focusing"),
        BotCommand(command="subjects", description="Add / rename subjects"),
        BotCommand(command="addsubject", description="Add a subject: /addsubject Physics"),
        BotCommand(command="renamesubject", description="Rename: /renamesubject <id> <New Name>"),
    ])
    log.info("Study bot online for admin %s", settings.admin_id)

    report_task = asyncio.create_task(daily_report_loop(bot, db, settings))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        report_task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bye 👋")
