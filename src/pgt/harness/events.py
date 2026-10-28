"""Append-only SQLite events shared by all workspace stores."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock

from pydantic import BaseModel


class Event(BaseModel):
    id: int
    ts: str
    sim_day: int | None
    loop_id: str | None
    actor: str
    type: str
    payload: dict
    refs: list[str]


class EventLog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = RLock()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, sim_day INTEGER, loop_id TEXT,
                actor TEXT NOT NULL, type TEXT NOT NULL,
                payload TEXT NOT NULL, refs TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update
            BEFORE UPDATE ON events BEGIN
                SELECT RAISE(ABORT, 'events are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete
            BEFORE DELETE ON events BEGIN
                SELECT RAISE(ABORT, 'events are append-only');
            END;
        """)

    @contextmanager
    def _transaction(self):
        """Let store mutations and their events commit together."""
        with self._lock:
            if self._conn.in_transaction:
                yield
            else:
                with self._conn:
                    yield

    def append(
        self, type: str, actor: str, payload: dict | None = None,
        refs: list[str] | None = None, sim_day: int | None = None,
        loop_id: str | None = None,
    ) -> Event:
        event = Event(
            id=0, ts=datetime.now(UTC).isoformat(), sim_day=sim_day,
            loop_id=loop_id, actor=actor, type=type,
            payload=payload if payload is not None else {},
            refs=refs if refs is not None else [],
        )
        with self._transaction():
            cursor = self._conn.execute(
                "INSERT INTO events (ts, sim_day, loop_id, actor, type, payload, refs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event.ts, sim_day, loop_id, actor, type,
                 json.dumps(event.payload), json.dumps(event.refs)),
            )
            event.id = cursor.lastrowid
        return event

    def list(
        self, type: str | None = None, loop_id: str | None = None, since_id: int = 0,
    ) -> list[Event]:
        clauses, args = ["id > ?"], [since_id]
        if type is not None:
            if type.endswith("."):
                clauses.append("substr(type, 1, ?) = ?")
                args.extend([len(type), type])
            else:
                clauses.append("type = ?")
                args.append(type)
        if loop_id is not None:
            clauses.append("loop_id = ?")
            args.append(loop_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, sim_day, loop_id, actor, type, payload, refs FROM events WHERE "
                + " AND ".join(clauses) + " ORDER BY id", args,
            ).fetchall()
        return [
            Event(id=r[0], ts=r[1], sim_day=r[2], loop_id=r[3], actor=r[4], type=r[5],
                  payload=json.loads(r[6]), refs=json.loads(r[7]))
            for r in rows
        ]
