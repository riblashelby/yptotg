"""Single-source configuration loaded from `.env` via pydantic-settings."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All bot configuration lives in exactly one `.env` file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_id: int
    report_channel_id: int | str
    report_time: str = "23:59"          # HH:MM, local time of the machine
    proxy_url: str | None = None        # e.g. socks5://user:pass@host:1080
    db_path: str = "study_timer.db"

    @property
    def report_minutes(self) -> int:
        """`REPORT_TIME` cast to minutes past midnight (raises on malformed input)."""
        hour_text, _, minute_text = self.report_time.partition(":")
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"Invalid REPORT_TIME: {self.report_time!r}")
        return hour * 60 + minute


@dataclass(frozen=True, slots=True)
class TimerConfig:
    """Session rules — injectable so tests can tune them freely."""

    subjects: tuple[str, ...] = ("Math", "Code", "Reading", "Language", "Other")
    max_session_seconds: int = 12 * 3600  # safety cap for an abandoned timer


DEFAULT_TIMER_CONFIG = TimerConfig()


def seconds_until(hhmm: str, now_hm: tuple[int, int]) -> int:
    """Seconds from `now_hm` (hour, minute) until the next occurrence of `hhmm`."""
    hour_text, _, minute_text = hhmm.partition(":")
    target = int(hour_text) * 60 + int(minute_text)
    now = now_hm[0] * 60 + now_hm[1]
    delta = target - now
    return (delta if delta > 0 else delta + 1440) * 60
