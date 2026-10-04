"""aiosqlite persistence layer: minimal schema + async CRUD. Zero ORM bloat."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT    NOT NULL,
    start_ts   REAL    NOT NULL,
    end_ts     REAL,
    paused_sec REAL    NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS active (
    chat_id    INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    subject    TEXT    NOT NULL,
    started_at REAL    NOT NULL,
    paused_sec REAL    NOT NULL DEFAULT 0,
    paused_at  REAL
);
"""


def _local_day(ts: float) -> str:
    """Local calendar day (YYYY-MM-DD) of a unix timestamp — Python-side, TZ-correct."""
    return datetime.fromtimestamp(ts).date().isoformat()


async def init_db(path: str) -> aiosqlite.Connection:
    """Open (and migrate) the database. Pass ':memory:' in tests."""
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    # Day bucketing is registered as a deterministic Python function instead of
    # SQLite's timezone-fragile DATE()/localtime modifiers.
    await db.create_function("local_day", 1, _local_day)
    await db.executescript(SCHEMA)
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# Finished sessions
# ---------------------------------------------------------------------------
async def create_session(db: Any, subject: str, start_ts: float) -> int:
    """Insert a running session row, return its id."""
    cursor = await db.execute(
        "INSERT INTO sessions (subject, start_ts) VALUES (?, ?)", (subject, start_ts)
    )
    await db.commit()
    return int(cursor.lastrowid)


async def finish_session(db: Any, session_id: int, end_ts: float) -> None:
    """Stamp the end time; net study time is derived as end_ts - start_ts - paused_sec."""
    await db.execute(
        "UPDATE sessions SET end_ts = ? WHERE id = ?", (end_ts, session_id)
    )
    await db.commit()


async def get_day_totals(db: Any, day: date) -> dict[str, float]:
    """{subject: net_seconds} for every session that ended on `day`."""
    cursor = await db.execute(
        """
        SELECT subject,
               SUM(end_ts - start_ts - paused_sec) AS total
        FROM sessions
        WHERE end_ts IS NOT NULL AND local_day(end_ts) = ?
        GROUP BY subject
        """,
        (day.isoformat(),),
    )
    rows = await cursor.fetchall()
    return {row["subject"]: float(row["total"]) for row in rows}


# ---------------------------------------------------------------------------
# The single live timer
# ---------------------------------------------------------------------------
async def start_active(db: Any, chat_id: int, session_id: int, subject: str, ts: float) -> None:
    await db.execute(
        """
        INSERT INTO active (chat_id, session_id, subject, started_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            session_id = excluded.session_id,
            subject    = excluded.subject,
            started_at = excluded.started_at,
            paused_sec = 0,
            paused_at  = NULL
        """,
        (chat_id, session_id, subject, ts),
    )
    await db.commit()


async def get_active(db: Any, chat_id: int) -> dict | None:
    cursor = await db.execute("SELECT * FROM active WHERE chat_id = ?", (chat_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def set_paused(db: Any, chat_id: int, paused_at: float | None, accrued: float) -> None:
    """paused_at=None resumes; `accrued` is the total paused seconds so far."""
    await db.execute(
        "UPDATE active SET paused_at = ?, paused_sec = ? WHERE chat_id = ?",
        (paused_at, accrued, chat_id),
    )
    await db.commit()


async def clear_active(db: Any, chat_id: int) -> None:
    await db.execute("DELETE FROM active WHERE chat_id = ?", (chat_id,))
    await db.commit()
