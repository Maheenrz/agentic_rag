"""
feedback_store.py
Thumbs up / thumbs down on an assistant answer.

one rating per (message, user); rating again REPLACES the old one

rating is stored as 1 (up) or -1 (down), with an optional reason + comment
"""

import time
from typing import Optional
from sqlalchemy import Float, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IS_PG, engine, connect

REASONS = ("wrong_answer", "missing_info", "bad_citation", "not_helpful", "other")

class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    org_id: Mapped[str] = mapped_column(String, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    comment: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_feedback_msg_user"),
        Index("idx_feedback_org", "org_id", "id"),
        Index("idx_feedback_thread", "thread_id"),
    )


def _init_db() -> None:
    Base.metadata.create_all(bind=engine, tables=[Feedback.__table__])

_init_db()

def set_feedback(message_id: int, thread_id: str, user_id: str, org_id: str,
                 rating: int, reason: str = "", comment: str = "") -> None:
    now = time.time()
    values = {
        "message_id": message_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "org_id": org_id,
        "rating": rating,
        "reason": reason,
        "comment": comment,
        "created_at": now,
        "updated_at": now,
    }

    with connect() as session:
        if IS_PG:
            stmt = pg_insert(Feedback).values(**values)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_feedback_msg_user",
                set_={
                    "rating": stmt.excluded.rating,
                    "reason": stmt.excluded.reason,
                    "comment": stmt.excluded.comment,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
        else:
            stmt = sqlite_insert(Feedback).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["message_id", "user_id"],
                set_={
                    "rating": stmt.excluded.rating,
                    "reason": stmt.excluded.reason,
                    "comment": stmt.excluded.comment,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
        session.execute(stmt)


def remove_feedback(message_id: int, user_id: str) -> bool:
    with connect() as session:
        res = session.execute(
            text("DELETE FROM feedback WHERE message_id = :message_id AND user_id = :user_id"),
            {"message_id": message_id, "user_id": user_id},
        )
        return res.rowcount > 0

def ratings_for_thread(thread_id: str, user_id: str) -> dict[int, dict]:
    with connect() as session:
        rows = session.execute(
            text("SELECT message_id, rating, reason, comment FROM feedback WHERE thread_id = :thread_id AND user_id = :user_id"),
            {"thread_id": thread_id, "user_id": user_id},
        ).mappings().fetchall()
        return {r["message_id"]: {"rating": r["rating"], "reason": r["reason"], "comment": r["comment"]} for r in rows}

def list_feedback(org_id: str, rating: Optional[int] = None, limit: int = 50, before_id: Optional[int] = None) -> list[dict]:
    q = "SELECT id, message_id, thread_id, user_id, rating, reason, comment, created_at FROM feedback WHERE org_id = :org_id"
    params = {"org_id": org_id}
    if rating in (1, -1):
        q += " AND rating = :rating"
        params["rating"] = rating
    if before_id:
        q += " AND id < :before_id"
        params["before_id"] = before_id
    q += " ORDER BY id DESC LIMIT :limit"
    params["limit"] = max(1, min(limit, 200))

    with connect() as session:
        return [dict(r) for r in session.execute(text(q), params).mappings().fetchall()]


def summary(org_id: str) -> dict:
    with connect() as session:
        up = session.execute(text("SELECT COUNT(*) AS c FROM feedback WHERE org_id = :org AND rating = 1"), {"org": org_id}).scalar() or 0
        down = session.execute(text("SELECT COUNT(*) AS c FROM feedback WHERE org_id = :org AND rating = -1"), {"org": org_id}).scalar() or 0
        reasons = session.execute(
            text("SELECT reason, COUNT(*) AS c FROM feedback WHERE org_id = :org AND rating = -1 AND reason != '' "
                 "GROUP BY reason ORDER BY c DESC"),
            {"org": org_id},
        ).mappings().fetchall()
        total = up + down
        return {
            "up": up, "down": down, "total": total,
            "satisfaction_pct": round(100 * up / total, 1) if total else None,
            "down_reasons": {r["reason"]: r["c"] for r in reasons},
        }

    
def delete_thread_feedback(thread_id: str) -> None:
    with connect() as session:
        session.execute(text("DELETE FROM feedback WHERE thread_id = :thread_id"), {"thread_id": thread_id})