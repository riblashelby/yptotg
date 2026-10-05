"""Tiny, idempotent schema migrations for study_timer.db.

Kept separate from database.py so the migration path is explicit and testable:
every step must be safe to run on a fresh DB *and* on every old DB in the wild.

v2: subjects become real rows (add/rename at runtime). Historical stats are
preserved by copying old text names into the registry — renames update only the
registry row, so past reports keep their original labels.
"""

from __future__ import annotations

import time as _time
from typing import Any

from config import DEFAULT_TIMER_CONFIG


async def migrate(db: Any) -> None:
    """Bring an existing v1 database up to the current schema (safe to re-run)."""
    cursor = await db.execute("PRAGMA user_version")
    version = int((await cursor.fetchone())[0])
    if version >= 2:
        return

    # Snapshot legacy state BEFORE the v2 SCHEMA replaces those tables.
    has_legacy = False
    legacy_sessions: list[tuple[str, float, float | None, float]] = []
    legacy_active: list[tuple[int, int, str, float, float, float | None]] = []
    try:
        cursor = await db.execute(
            "SELECT subject, start_ts, end_ts, paused_sec FROM sessions"
        )
        legacy_sessions = [
            (row["subject"], row["start_ts"], row["end_ts"], row["paused_sec"])
            for row in await cursor.fetchall()
        ]
        cursor = await db.execute(
            "SELECT chat_id, session_id, subject, started_at, paused_sec, paused_at FROM active"
        )
        legacy_active = [
            (
                row["chat_id"], row["session_id"], row["subject"],
                row["started_at"], row["paused_sec"], row["paused_at"],
            )
            for row in await cursor.fetchall()
        ]
        has_legacy = True
    except Exception:
        pass  # no legacy tables yet — nothing to carry over

    import database as db_api

    await db.executescript(db_api.SCHEMA)  # subjects + v2 sessions/active

    # Seed the registry: built-in defaults plus every historical name.
    names = list(DEFAULT_TIMER_CONFIG.subjects)
    names += [row[0] for row in legacy_sessions]
    names += [row[2] for row in legacy_active]
    now = _time.time()
    unique_names = sorted(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    await db.executemany(
        "INSERT OR IGNORE INTO subjects (name, created_at) VALUES (?, ?)",
        [(name, now) for name in unique_names],
    )
    cursor = await db.execute("SELECT id, name FROM subjects")
    ids = {row["name"]: row["id"] for row in await cursor.fetchall()}

    if has_legacy:
        # Legacy tables still hold the old text schema; rebuild them as v2 twins.
        await db.executescript(
            """
            DROP TABLE IF EXISTS sessions;
            CREATE TABLE sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id INTEGER NOT NULL REFERENCES subjects(id),
                start_ts   REAL    NOT NULL,
                end_ts     REAL,
                paused_sec REAL    NOT NULL DEFAULT 0
            );
            DROP TABLE IF EXISTS active;
            CREATE TABLE active (
                chat_id    INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL,
                subject_id INTEGER NOT NULL REFERENCES subjects(id),
                started_at REAL    NOT NULL,
                paused_sec REAL    NOT NULL DEFAULT 0,
                paused_at  REAL
            );
            """
        )
        if legacy_sessions:
            await db.executemany(
                "INSERT INTO sessions (subject_id, start_ts, end_ts, paused_sec) VALUES (?, ?, ?, ?)",
                [
                    (ids[subject], start_ts, end_ts, paused_sec)
                    for subject, start_ts, end_ts, paused_sec in legacy_sessions
                ],
            )
        rebuilt_active: list[tuple[int, int, int, float, float, float | None]] = []
        for chat_id, session_id, subject, started_at, paused_sec, paused_at in legacy_active:
            if subject not in ids:
                continue
            # Old session ids may shift; re-point at the session with the same start_ts.
            found = await (
                await db.execute(
                    "SELECT id FROM sessions WHERE subject_id = ? AND start_ts = ? LIMIT 1",
                    (ids[subject], started_at),
                )
            ).fetchone()
            new_sid = found["id"] if found else session_id
            rebuilt_active.append((chat_id, new_sid, ids[subject], started_at, paused_sec, paused_at))
        if rebuilt_active:
            await db.executemany(
                "INSERT INTO active (chat_id, session_id, subject_id, started_at, paused_sec, paused_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rebuilt_active,
            )

    await db.execute("PRAGMA user_version = 2")
    await db.commit()
