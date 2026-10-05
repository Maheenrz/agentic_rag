"""
audit_store.py
---------------
Append-only audit trail (same SQLite file). Answers "who did what, when" for
admin actions, logins, uploads, and security events (blocked questions,
suspicious documents). log() NEVER raises: a logging failure must not break
the request it is describing.

Privacy: we store the first 200 chars of a flagged question, never full chat
content for ordinary requests.
"""

import json
import sqlite3
import time
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
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                org_id TEXT, user_id TEXT, username TEXT,
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                ip TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_org_ts ON audit_log (org_id, ts)")


_init_db()


def log(action: str, user: Optional[dict] = None, target: str = "", detail: Optional[dict] = None,
        ip: str = "", org_id: Optional[str] = None, username: Optional[str] = None) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, org_id, user_id, username, action, target, detail_json, ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), org_id or (user or {}).get("org_id"), (user or {}).get("user_id"),
                 username or (user or {}).get("username"), action, target,
                 json.dumps(detail or {}, default=str)[:4000], ip),
            )
    except Exception:
        logger.exception("audit log write failed for action %s", action)


def list_events(org_id: str, limit: int = 100, action: Optional[str] = None, before_id: Optional[int] = None) -> list[dict]:
    q = "SELECT id, ts, user_id, username, action, target, detail_json, ip FROM audit_log WHERE org_id = ?"
    args: list = [org_id]
    if action:
        q += " AND action = ?"; args.append(action)
    if before_id:
        q += " AND id < ?"; args.append(before_id)
    q += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(limit, 500)))
    with _connect() as conn:
        rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["detail"] = json.loads(d.pop("detail_json") or "{}"); out.append(d)
    return out