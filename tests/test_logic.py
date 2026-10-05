"""Unit tests for the pure logic layer."""
from __future__ import annotations

from datetime import date

import pytest

import logic
from config import Settings, TimerConfig, seconds_until


# --------------------------------------------------------------------------- timer math
def test_elapsed_running():
    assert logic.elapsed_seconds(1000.0, 1300.0, 0.0, None) == 300.0


def test_elapsed_subtracts_accrued_pause():
    assert logic.elapsed_seconds(1000.0, 1500.0, 200.0, None) == 300.0


def test_elapsed_while_currently_paused():
    # started 1000, paused at 1200, now 1500 -> only 200s of focus counted
    assert logic.elapsed_seconds(1000.0, 1500.0, 0.0, 1200.0) == 200.0


def test_elapsed_never_negative():
    assert logic.elapsed_seconds(1000.0, 900.0, 0.0, None) == 0.0


def test_new_paused_state_pauses_then_resumes():
    paused_at, accrued = logic.new_paused_state(None, 500.0, 60.0)   # running -> pause
    assert (paused_at, accrued) == (500.0, 60.0)

    paused_at, accrued = logic.new_paused_state(400.0, 500.0, 60.0)  # paused -> resume
    assert (paused_at, accrued) == (None, 160.0)


# --------------------------------------------------------------------------- formatting
@pytest.mark.parametrize(
    "seconds, expected",
    [(0, "0:00"), (65, "1:05"), (599, "9:59"), (3725, "1:02:05"), (3660, "1:01:00")],
)
def test_format_stopwatch(seconds, expected):
    assert logic.format_stopwatch(seconds) == expected


@pytest.mark.parametrize(
    "seconds, expected",
    [(45, "45s"), (90, "1m 30s"), (3725, "1h 02m"), (7200, "2h 00m")],
)
def test_format_duration(seconds, expected):
    assert logic.format_duration(seconds) == expected


def test_rank_subjects_descending():
    ranked = logic.rank_subjects({"Math": 100, "Code": 300, "Reading": 200})
    assert ranked == [("Code", 300), ("Reading", 200), ("Math", 100)]


def test_top_subject():
    assert logic.top_subject({"Math": 10, "Code": 90}) == "Code"
    assert logic.top_subject({}) is None


@pytest.mark.parametrize(
    "fraction, expected",
    [(0.0, "▱▱▱▱▱▱▱▱"), (0.5, "▰▰▰▰▱▱▱▱"), (1.0, "▰▰▰▰▰▰▰▰"), (1.5, "▰▰▰▰▰▰▰▰")],
)
def test_progress_bar(fraction, expected):
    assert logic.progress_bar(fraction) == expected


# --------------------------------------------------------------------------- report
def test_render_daily_report_with_data():
    day = date(2026, 10, 5)
    report = logic.render_daily_report({"Math": 3600, "Code": 7200}, day)

    assert "Daily Study Report" in report
    assert "Monday, 05 October 2026" in report
    assert "3h 00m" in report            # total (2h + 1h)
    assert "Math" in report and "Code" in report
    assert "67%" in report and "33%" in report   # shares of the 3h total
    assert report.index("Code") < report.index("Math")  # sorted by time desc


def test_render_daily_report_empty_day():
    report = logic.render_daily_report({}, date(2026, 10, 5))
    assert "No study sessions" in report
    assert "<b>" in report  # HTML stays valid even on empty days


def test_render_status_states():
    assert "Focusing" in logic.render_status("Math", 65, paused=False)
    assert "Paused" in logic.render_status("Math", 65, paused=True)
    assert "1:05" in logic.render_status("Math", 65, paused=False)


# --------------------------------------------------------------------------- config
def make_settings(**overrides) -> Settings:
    base = {"bot_token": "42:TEST", "admin_id": 1, "report_channel_id": -100, "_env_file": None}
    return Settings(**{**base, **overrides})


def test_settings_defaults_and_casting():
    settings = make_settings()
    assert settings.report_time == "23:59"
    assert settings.report_minutes == 23 * 60 + 59
    assert settings.proxy_url in (None, "")


def test_settings_invalid_report_time():
    with pytest.raises(ValueError):
        make_settings(report_time="99:99").report_minutes


@pytest.mark.parametrize(
    "hhmm, now_hm, expected",
    [("23:59", (23, 58), 60), ("00:00", (23, 59), 60), ("12:00", (12, 0), 86400)],
)
def test_seconds_until(hhmm, now_hm, expected):
    assert seconds_until(hhmm, now_hm) == expected


def test_timer_config_is_injectable():
    custom = TimerConfig(subjects=("A", "B"))
    assert custom.subjects == ("A", "B")
