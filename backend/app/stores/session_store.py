"""
session_store.py
Persistent chat threads and their messages, managed with SQLAlchemy.
"""

import json
import time
import uuid
from typing import Optional
from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import logger
from app.db import Base, IS_PG, engine, connect


class Thread(Base):
    __tablename__ = "threads"
    thread_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False, server_default="New chat")
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (Index("idx_threads_user", "user_id"),)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String, ForeignKey("threads.thread_id"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    trace_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    sources_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    created_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (Index("idx_messages_thread", "thread_id"),)



from app.db import Base, engine, SessionLocal, connect

def _init_db() -> None:
    if engine:
        Base.metadata.create_all(bind=engine, tables=[Thread.__table__, Message.__table__])

_init_db()

def create_thread(user_id: str) -> str:
    thread_id = uuid.uuid4().hex
    now = time.time()
    with SessionLocal() as session:
        t = Thread(
            thread_id=thread_id,
            user_id=user_id,
            title="New chat",
            created_at=now,
            updated_at=now,
        )
        session.add(t)
        session.commit()
    logger.info("Created new thread: %s (user %s)", thread_id, user_id)
    return thread_id

def add_message(
    thread_id: str,
    role: str,
    content: str,
    trace: Optional[list] = None,
    sources: Optional[list] = None,
) -> int:
    now = time.time()
    msg = Message(
        thread_id=thread_id,
        role=role,
        content=content,
        trace_json=json.dumps(trace or []),
        sources_json=json.dumps(sources or []),
        created_at=now,
    )
    with SessionLocal() as session:
        session.add(msg)
        session.flush()
        new_id = msg.id
        session.execute(
            text("UPDATE threads SET updated_at = :updated_at WHERE thread_id = :thread_id"),
            {"updated_at": now, "thread_id": thread_id},
        )
        session.commit()
    return new_id





def _row_to_message(row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "trace": json.loads(row["trace_json"]),
        "sources": json.loads(row["sources_json"]),
    }




def thread_belongs_to_user(thread_id: str, user_id: str) -> bool:
    with connect() as session:
        row = session.execute(
            text("SELECT 1 FROM threads WHERE thread_id = :thread_id AND user_id = :user_id"),
            {"thread_id": thread_id, "user_id": user_id},
        ).fetchone()
        return row is not None


def rename_thread(thread_id: str, title: str) -> None:
    title = title.strip()[:80] or "New chat"
    with connect() as session:
        session.execute(
            text("UPDATE threads SET title = :title, updated_at = :updated_at WHERE thread_id = :thread_id"),
            {"title": title, "updated_at": time.time(), "thread_id": thread_id},
        )




def get_messages(thread_id: str) -> list[dict]:
    with connect() as session:
        rows = session.execute(
            text("SELECT id, role, content, trace_json, sources_json FROM messages "
                 "WHERE thread_id = :thread_id ORDER BY id"),
            {"thread_id": thread_id},
        ).mappings().fetchall()
        return [_row_to_message(r) for r in rows]


def get_message(thread_id: str, message_id: int) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text("SELECT id, role, content, trace_json, sources_json FROM messages "
                 "WHERE id = :message_id AND thread_id = :thread_id"),
            {"message_id": message_id, "thread_id": thread_id},
        ).mappings().fetchone()
        return _row_to_message(row) if row else None


def get_question_for_answer(thread_id: str, answer_message_id: int) -> str:
    with connect() as session:
        row = session.execute(
            text("SELECT content FROM messages WHERE thread_id = :thread_id AND role = 'user' AND id < :ans_id "
                 "ORDER BY id DESC LIMIT 1"),
            {"thread_id": thread_id, "ans_id": answer_message_id},
        ).mappings().fetchone()
        return row["content"] if row else ""


def list_threads(user_id: str, limit: int = 30) -> list[dict]:
    with connect() as session:
        rows = session.execute(
            text("SELECT thread_id, title, created_at, updated_at FROM threads "
                 "WHERE user_id = :user_id ORDER BY updated_at DESC LIMIT :limit"),
            {"user_id": user_id, "limit": limit},
        ).mappings().fetchall()
        return [dict(r) for r in rows]


def list_all_thread_ids(user_id: str) -> list[str]:
    with connect() as session:
        rows = session.execute(
            text("SELECT thread_id FROM threads WHERE user_id = :user_id"),
            {"user_id": user_id},
        ).mappings().fetchall()
        return [r["thread_id"] for r in rows]


def thread_message_count(thread_id: str) -> int:
    with connect() as session:
        count = session.execute(
            text("SELECT COUNT(*) AS c FROM messages WHERE thread_id = :thread_id"),
            {"thread_id": thread_id},
        ).scalar()
        return count or 0


def delete_thread(thread_id: str) -> None:
    with connect() as session:
        session.execute(text("DELETE FROM messages WHERE thread_id = :thread_id"), {"thread_id": thread_id})
        session.execute(text("DELETE FROM threads WHERE thread_id = :thread_id"), {"thread_id": thread_id})