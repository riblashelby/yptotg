"""Pure business logic: timer math + report formatting. No I/O, trivially testable."""
from __future__ import annotations

from datetime import date

# ---------------------------------------------------------------------------
# Timer math
# ---------------------------------------------------------------------------
def elapsed_seconds(started_at: float, now: float, paused_sec: float, paused_at: float | None) -> float:
    """Net focused seconds — time spent paused never counts."""
    running = max(now - started_at - paused_sec, 0.0)
    if paused_at is not None:
        running -= now - paused_at
    return max(running, 0.0)


def new_paused_state(paused_at: float | None, now: float, paused_sec: float) -> tuple[float, float]:
    """Pause a running timer -> (paused_at, accrued). Resume a paused one -> (None, accrued)."""
    if paused_at is None:                      # currently running -> pause
        return now, paused_sec
    return None, paused_sec + (now - paused_at)  # currently paused  -> resume


def format_stopwatch(seconds: float) -> str:
    """125 -> '2:05', 3725 -> '1:02:05'."""
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_duration(seconds: float) -> str:
    """Human-friendly block: 3725 -> '1h 02m', 90 -> '1m 30s'."""
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def rank_subjects(totals: dict[str, float]) -> list[tuple[str, float]]:
    """Subjects sorted by time, descending."""
    return sorted(totals.items(), key=lambda item: item[1], reverse=True)


def top_subject(totals: dict[str, float]) -> str | None:
    ranked = rank_subjects(totals)
    return ranked[0][0] if ranked else None


def progress_bar(fraction: float, width: int = 8) -> str:
    """Text-based bar: ▰▰▰▱▱▱▱▱"""
    filled = round(max(0.0, min(fraction, 1.0)) * width)
    return "▰" * filled + "▱" * (width - filled)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def render_daily_report(day_totals: dict[str, float], day: date) -> str:
    """Premium-looking daily summary sent to the channel."""
    pretty_day = day.strftime("%A, %d %B %Y")
    total = sum(day_totals.values())

    if total <= 0:
        return (
            f"🌙 <b>Daily Study Report</b>\n"
            f"<i>{pretty_day}</i>\n\n"
            f"✨ No study sessions logged today.\n"
            f"Rest well — tomorrow is a fresh page. 📖"
        )

    lines = [
        f"🌙 <b>Daily Study Report</b>",
        f"<i>{pretty_day}</i>",
        "",
        f"⏱ Total focus · <b>{format_duration(total)}</b>",
        "",
    ]
    for subject, seconds in rank_subjects(day_totals):
        share = seconds / total
        lines.append(f"{progress_bar(share)}  <b>{subject}</b>")
        lines.append(f"               {format_duration(seconds)} · {share:.0%}")
        lines.append("")
    lines.append("🔥 Consistency compounds. See you tomorrow ✨")
    return "\n".join(lines).rstrip()


def render_status(subject: str, seconds: float, paused: bool) -> str:
    """Live timer card text."""
    icon = "⏸" if paused else "▶️"
    state = "Paused" if paused else "Focusing"
    return (
        f"{icon} <b>{state}</b> · {subject}\n\n"
        f"⏳ <code>{format_stopwatch(seconds)}</code>"
    )
