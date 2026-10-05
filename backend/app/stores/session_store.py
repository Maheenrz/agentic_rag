"""
session_store.py
-----------------
Persistent chat threads and their messages, stored in SQLite.

  threads   one row per chat, owned by one user
  messages  one row per message, with the reasoning trace and sources as JSON

Every thread-scoped API call must first check thread_belongs_to_user().
"""

import json
import sqlite3
import time
import uuid
import os
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

from app.config import SQLITE_DB_PATH, logger


db_dir = os.path.dirname(SQLITE_DB_PATH)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)


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
                user_id TEXT NOT NULL,
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_user ON threads (user_id)")


_init_db()


def _row_to_message(row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "trace": json.loads(row["trace_json"]),
        "sources": json.loads(row["sources_json"]),
    }


def create_thread(user_id: str) -> str:
    thread_id = uuid.uuid4().hex
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO threads (thread_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (thread_id, user_id, "New chat", now, now),
        )
    logger.info("Created new thread: %s (user %s)", thread_id, user_id)
    return thread_id


def thread_belongs_to_user(thread_id: str, user_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = ? AND user_id = ?",
            (thread_id, user_id),
        ).fetchone()
    return row is not None


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
) -> int:
    """Stores one message and returns its id (the frontend needs it for feedback)."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO messages (thread_id, role, content, trace_json, sources_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (thread_id, role, content, json.dumps(trace or []), json.dumps(sources or []), time.time()),
        )
        conn.execute("UPDATE threads SET updated_at = ? WHERE thread_id = ?", (time.time(), thread_id))
        return cur.lastrowid


def get_messages(thread_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, role, content, trace_json, sources_json FROM messages "
            "WHERE thread_id = ? ORDER BY id",
            (thread_id,),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def get_message(thread_id: str, message_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, role, content, trace_json, sources_json FROM messages "
            "WHERE id = ? AND thread_id = ?",
            (message_id, thread_id),
        ).fetchone()
    return _row_to_message(row) if row else None


def get_question_for_answer(thread_id: str, answer_message_id: int) -> str:
    """The user message written just before this assistant message."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT content FROM messages WHERE thread_id = ? AND role = 'user' AND id < ? "
            "ORDER BY id DESC LIMIT 1",
            (thread_id, answer_message_id),
        ).fetchone()
    return row["content"] if row else ""


def list_threads(user_id: str, limit: int = 30) -> list[dict]:
    """Most recently active threads first, scoped to this user only."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT thread_id, title, created_at, updated_at FROM threads "
            "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_all_thread_ids(user_id: str) -> list[str]:
    """Every thread this user owns, with no limit. Used when a user is deleted."""
    with _connect() as conn:
        rows = conn.execute("SELECT thread_id FROM threads WHERE user_id = ?", (user_id,)).fetchall()
    return [r["thread_id"] for r in rows]


def thread_message_count(thread_id: str) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE thread_id = ?", (thread_id,)).fetchone()
    return row["c"]


def delete_thread(thread_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
        conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))