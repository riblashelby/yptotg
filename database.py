"""aiosqlite persistence layer: minimal schema + async CRUD. Zero ORM bloat.

v2: subjects are real rows — they can be added/renamed at runtime. Sessions and
the live timer reference ``subject_id``; reports JOIN against ``subjects`` so a
rename instantly updates every historical row's label (display-name semantics).
"""
from __future__ import annotations

import time as _time
from datetime import date
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL COLLATE NOCASE UNIQUE,
    created_at REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    start_ts   REAL    NOT NULL,
    end_ts     REAL,
    paused_sec REAL    NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS active (
    chat_id    INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    started_at REAL    NOT NULL,
    paused_sec REAL    NOT NULL DEFAULT 0,
    paused_at  REAL
);
"""


def _local_day(ts: float) -> str:
    """Local calendar day (YYYY-MM-DD) of a unix timestamp — Python-side, TZ-correct."""
    return date.fromtimestamp(ts).isoformat()


async def init_db(path: str) -> aiosqlite.Connection:
    """Open (and migrate) the database. Pass ':memory:' in tests."""
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    # Day bucketing is registered as a deterministic Python function instead of
    # SQLite's timezone-fragile DATE()/localtime modifiers.
    await db.create_function("local_day", 1, _local_day)
    await db.executescript(SCHEMA)
    await db.commit()

    cursor = await db.execute("SELECT COUNT(*) AS n FROM subjects")
    seeded = (await cursor.fetchone())["n"] > 0
    if not seeded:  # first run on a fresh DB — backfills legacy data too
        from migrations import migrate  # local import breaks the module cycle

        await migrate(db)
    return db


# ---------------------------------------------------------------------------
# Subject registry
# ---------------------------------------------------------------------------
def normalize_subject(name: str) -> str:
    """Trim inner whitespace, collapse repeats, cap length. '' if nothing left."""
    return " ".join(name.split())[:40]


async def list_subjects(db: Any) -> list[tuple[int, str]]:
    """(id, name) pairs, alphabetical — stable keyboard order."""
    cursor = await db.execute("SELECT id, name FROM subjects ORDER BY name COLLATE NOCASE")
    return [(row["id"], row["name"]) for row in await cursor.fetchall()]


async def get_subject(db: Any, subject_id: int) -> tuple[int, str] | None:
    cursor = await db.execute("SELECT id, name FROM subjects WHERE id = ?", (subject_id,))
    row = await cursor.fetchone()
    return (row["id"], row["name"]) if row else None


async def find_subject(db: Any, name: str) -> tuple[int, str] | None:
    cursor = await db.execute(
        "SELECT id, name FROM subjects WHERE name = ? COLLATE NOCASE", (normalize_subject(name),)
    )
    row = await cursor.fetchone()
    return (row["id"], row["name"]) if row else None


async def add_subject(db: Any, name: str) -> tuple[int, str]:
    """Insert a subject. Raises ValueError on empty/duplicate names."""
    clean = normalize_subject(name)
    if not clean:
        raise ValueError("Subject name is empty")
    try:
        cursor = await db.execute(
            "INSERT INTO subjects (name, created_at) VALUES (?, ?)", (clean, _time.time())
        )
    except aiosqlite.IntegrityError as exc:
        raise ValueError(f"Subject {clean!r} already exists") from exc
    await db.commit()
    return int(cursor.lastrowid), clean


async def rename_subject(db: Any, subject_id: int, new_name: str) -> tuple[int, str]:
    """Rename in place — old stats keep their history, labels update everywhere."""
    clean = normalize_subject(new_name)
    if not clean:
        raise ValueError("Subject name is empty")
    try:
        cursor = await db.execute("UPDATE subjects SET name = ? WHERE id = ?", (clean, subject_id))
    except aiosqlite.IntegrityError as exc:
        raise ValueError(f"Subject {clean!r} already exists") from exc
    if cursor.rowcount == 0:
        raise KeyError(f"No subject #{subject_id}")
    await db.commit()
    return subject_id, clean


# ---------------------------------------------------------------------------
# Finished sessions
# ---------------------------------------------------------------------------
async def create_session(db: Any, subject_id: int, start_ts: float) -> int:
    """Insert a running session row, return its id."""
    cursor = await db.execute(
        "INSERT INTO sessions (subject_id, start_ts) VALUES (?, ?)", (subject_id, start_ts)
    )
    await db.commit()
    return int(cursor.lastrowid)


async def finish_session(db: Any, session_id: int, end_ts: float, paused_sec: float = 0.0) -> None:
    """Stamp the end time AND the accrued pause; net time = end_ts - start_ts - paused_sec."""
    await db.execute(
        "UPDATE sessions SET end_ts = ?, paused_sec = ? WHERE id = ?",
        (end_ts, paused_sec, session_id),
    )
    await db.commit()


async def get_day_totals(db: Any, day: date) -> dict[str, float]:
    """{subject name: net_seconds} for every session that ended on `day`."""
    cursor = await db.execute(
        """
        SELECT s.name AS name,
               SUM(t.end_ts - t.start_ts - t.paused_sec) AS total
        FROM sessions AS t
        JOIN subjects AS s ON s.id = t.subject_id
        WHERE t.end_ts IS NOT NULL AND local_day(t.end_ts) = ?
        GROUP BY s.name
        """,
        (day.isoformat(),),
    )
    rows = await cursor.fetchall()
    return {row["name"]: float(row["total"]) for row in rows}


# ---------------------------------------------------------------------------
# The single live timer
# ---------------------------------------------------------------------------
async def start_active(db: Any, chat_id: int, session_id: int, subject_id: int, ts: float) -> None:
    await db.execute(
        """
        INSERT INTO active (chat_id, session_id, subject_id, started_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            session_id = excluded.session_id,
            subject_id = excluded.subject_id,
            started_at = excluded.started_at,
            paused_sec = 0,
            paused_at  = NULL
        """,
        (chat_id, session_id, subject_id, ts),
    )
    await db.commit()


async def get_active(db: Any, chat_id: int) -> dict | None:
    """Live timer joined with its display name under key ``subject``."""
    cursor = await db.execute(
        """
        SELECT a.*, s.name AS subject
        FROM active AS a
        JOIN subjects AS s ON s.id = a.subject_id
        WHERE a.chat_id = ?
        """,
        (chat_id,),
    )
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
