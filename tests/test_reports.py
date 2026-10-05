"""End-to-end test for the automated daily channel report."""
from __future__ import annotations

import time
from datetime import date

import pytest

import database as db_api
import logic
from bot import send_daily_report
from config import Settings
from tests.conftest import CHANNEL_ID


def make_settings(**overrides) -> Settings:
    base = {"bot_token": "42:TEST", "admin_id": 1, "report_channel_id": CHANNEL_ID, "_env_file": None}
    return Settings(**{**base, **overrides})


async def test_send_daily_report_posts_totals_to_channel(db, mock_bot):
    now = time.time()
    ids = {name: sid for sid, name in await db_api.list_subjects(db)}
    for subject, seconds in [("Math", 5400), ("Code", 1800)]:
        sid = await db_api.create_session(db, ids[subject], now - seconds)
        await db_api.finish_session(db, sid, now)

    report = await send_daily_report(mock_bot, db, CHANNEL_ID, date.today())

    assert mock_bot.sent == [(CHANNEL_ID, report)]
    assert "Daily Study Report" in report
    assert "2h 00m" in report and "1h 30m" in report and "75%" in report


async def test_send_daily_report_empty_day_still_posts(db, mock_bot):
    report = await send_daily_report(mock_bot, db, CHANNEL_ID, date.today())
    assert "No study sessions" in report
    assert len(mock_bot.sent) == 1


def test_report_time_casting():
    assert make_settings().report_minutes == 23 * 60 + 59
    assert make_settings(report_time="07:30").report_minutes == 450
