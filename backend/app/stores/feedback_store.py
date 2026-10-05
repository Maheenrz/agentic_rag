"""
feedback_store.py
------------------
Thumbs up / thumbs down on an assistant answer.

  - one rating per (message, user); rating again REPLACES the old one
  - rating is stored as 1 (up) or -1 (down), with an optional reason + comment
  - we store only ids here. The question/answer text is read from the messages
    table when an admin looks at it, so deleting a chat also deletes what was
    said (feedback rows for that thread are removed too).
"""

import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from app.config import SQLITE_DB_PATH

REASONS = ("wrong_answer", "missing_info", "bad_citation", "not_helpful", "other")


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
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                org_id TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK (rating IN (1, -1)),
                reason TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (message_id, user_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_org ON feedback (org_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_thread ON feedback (thread_id)")


_init_db()


def set_feedback(message_id: int, thread_id: str, user_id: str, org_id: str,
                 rating: int, reason: str = "", comment: str = "") -> None:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO feedback (message_id, thread_id, user_id, org_id, rating, reason, comment, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (message_id, user_id)
               DO UPDATE SET rating = excluded.rating, reason = excluded.reason,
                             comment = excluded.comment, updated_at = excluded.updated_at""",
            (message_id, thread_id, user_id, org_id, rating, reason, comment, now, now),
        )


def remove_feedback(message_id: int, user_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM feedback WHERE message_id = ? AND user_id = ?", (message_id, user_id))
        return cur.rowcount > 0


def ratings_for_thread(thread_id: str, user_id: str) -> dict[int, dict]:
    """{message_id: {"rating": 1|-1, "reason": ..., "comment": ...}} -- so the UI can show a pressed thumb."""
    with _connect() as conn:
        rows = conn.execute("SELECT message_id, rating, reason, comment FROM feedback WHERE thread_id = ? AND user_id = ?",
                            (thread_id, user_id)).fetchall()
    return {r["message_id"]: {"rating": r["rating"], "reason": r["reason"], "comment": r["comment"]} for r in rows}


def list_feedback(org_id: str, rating: Optional[int] = None, limit: int = 50, before_id: Optional[int] = None) -> list[dict]:
    q = ("SELECT id, message_id, thread_id, user_id, rating, reason, comment, created_at FROM feedback WHERE org_id = ?")
    args: list = [org_id]
    if rating in (1, -1):
        q += " AND rating = ?"; args.append(rating)
    if before_id:
        q += " AND id < ?"; args.append(before_id)
    q += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(limit, 200)))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def summary(org_id: str) -> dict:
    with _connect() as conn:
        up, down = (conn.execute("SELECT COUNT(*) AS c FROM feedback WHERE org_id = ? AND rating = ?", (org_id, r)).fetchone()["c"]
                    for r in (1, -1))
        reasons = conn.execute("SELECT reason, COUNT(*) AS c FROM feedback WHERE org_id = ? AND rating = -1 AND reason != '' "
                               "GROUP BY reason ORDER BY c DESC", (org_id,)).fetchall()
    total = up + down
    return {"up": up, "down": down, "total": total,
            "satisfaction_pct": round(100 * up / total, 1) if total else None,
            "down_reasons": {r["reason"]: r["c"] for r in reasons}}


def delete_thread_feedback(thread_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM feedback WHERE thread_id = ?", (thread_id,))