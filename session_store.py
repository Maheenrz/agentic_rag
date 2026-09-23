"""
session_store.py
-----------------
Persistent storage for chat threads and their messages, using SQLite.

This is what turns "session" (a live browser connection, gone on refresh)
into "thread" (a named, resumable conversation saved to disk). "New chat"
creates a genuinely new persisted thread; the sidebar history list reads
straight from here.
"""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from config import SQLITE_DB_PATH, logger


@contextmanager
def _connect():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New chat',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                trace_json TEXT NOT NULL DEFAULT '[]',
                sources_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages (thread_id)")


_init_db()


def create_thread() -> str:
    thread_id = uuid.uuid4().hex
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO threads (thread_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (thread_id, "New chat", now, now),
        )
    logger.info("Created new thread: %s", thread_id)
    return thread_id


def rename_thread(thread_id: str, title: str) -> None:
    title = title.strip()[:80] or "New chat"
    with _connect() as conn:
        conn.execute(
            "UPDATE threads SET title = ?, updated_at = ? WHERE thread_id = ?",
            (title, time.time(), thread_id),
        )


def add_message(
    thread_id: str,
    role: str,
    content: str,
    trace: Optional[list] = None,
    sources: Optional[list] = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO messages (thread_id, role, content, trace_json, sources_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (thread_id, role, content, json.dumps(trace or []), json.dumps(sources or []), time.time()),
        )
        conn.execute("UPDATE threads SET updated_at = ? WHERE thread_id = ?", (time.time(), thread_id))


def get_messages(thread_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content, trace_json, sources_json FROM messages "
            "WHERE thread_id = ? ORDER BY id",
            (thread_id,),
        ).fetchall()
    return [
        {
            "role": row["role"],
            "content": row["content"],
            "trace": json.loads(row["trace_json"]),
            "sources": json.loads(row["sources_json"]),
        }
        for row in rows
    ]


def list_threads(limit: int = 30) -> list[dict]:
    """Most recently active threads first, for the history sidebar."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT thread_id, title, created_at, updated_at FROM threads "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def thread_message_count(thread_id: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    return row["c"]