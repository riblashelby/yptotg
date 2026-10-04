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
from aiogram.enums import ParseMode
from aiohttp_socks import ProxyConnector

import database as db_api
import logic
from config import Settings
from handlers import router
from middleware import AdminOnlyMiddleware

log = logging.getLogger("study-bot")


def build_bot(settings: Settings) -> Bot:
    """Bot factory — SOCKS5 wired via a custom aiohttp connector when configured."""
    kwargs: dict[str, Any] = {"default": DefaultBotProperties(parse_mode=ParseMode.HTML)}
    if settings.proxy_url:
        log.info("Using proxy: %s", settings.proxy_url.split("@")[-1])
        from aiogram.client.session.aiohttp import AiohttpSession

        kwargs["session"] = AiohttpSession(
            connector=ProxyConnector.from_url(settings.proxy_url)
        )
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

    await bot.set_my_commands([])
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
