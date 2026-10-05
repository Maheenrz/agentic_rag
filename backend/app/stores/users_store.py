"""
users_store.py
---------------
Persistent user accounts (same SQLite file as session_store.py).
CHANGED FOR ORGS: every user now belongs to one org and has a role
('admin' or 'member'). Old accounts get org_id = NULL until their first
request, when main.py gives them a personal org (see get_current_user).
"""

import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from app.config import SQLITE_DB_PATH, logger


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
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        # Migration for databases created before orgs existed.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "org_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN org_id TEXT")
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")


_init_db()

_COLS = "user_id, username, password_hash, org_id, role"


def create_user(username: str, password_hash: str, org_id: Optional[str] = None, role: str = "member") -> str:
    user_id = uuid.uuid4().hex
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, password_hash, created_at, org_id, role) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, password_hash, time.time(), org_id, role),
        )
    logger.info("Created user: %s (org %s, role %s)", username, org_id, role)
    return user_id


def get_user_by_username(username: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {_COLS} FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {_COLS} FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def username_exists(username: str) -> bool:
    return get_user_by_username(username) is not None


def set_user_org(user_id: str, org_id: str, role: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET org_id = ?, role = ? WHERE user_id = ?", (org_id, role, user_id))


def list_org_users(org_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, username, role FROM users WHERE org_id = ? ORDER BY username", (org_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_admins(org_id: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE org_id = ? AND role = 'admin'", (org_id,)
        ).fetchone()
    return row["c"]


def delete_user(user_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))