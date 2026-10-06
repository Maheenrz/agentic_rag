"""
users_store.py
Persistent user accounts managed with SQLAlchemy.
"""

import time
import uuid
from typing import Optional
from sqlalchemy import Float, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import logger
from app.db import Base, engine, connect, table_columns

class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    org_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False, server_default="member")


from app.db import Base, engine, SessionLocal, table_columns

def _init_db() -> None:
    if engine:
        Base.metadata.create_all(bind=engine, tables=[User.__table__])
        cols = table_columns("users")
        with engine.begin() as conn:
            if "org_id" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN org_id TEXT"))
            if "role" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'"))

_init_db()

def create_user(username: str, password_hash: str, org_id: Optional[str] = None, role: str = "member") -> str:
    user_id = uuid.uuid4().hex
    with SessionLocal() as session:
        u = User(
            user_id=user_id,
            username=username,
            password_hash=password_hash,
            created_at=time.time(),
            org_id=org_id,
            role=role,
        )
        session.add(u)
        session.commit()
    logger.info("Created user: %s (org %s, role %s)", username, org_id, role)
    return user_id


_COLS = "user_id, username, password_hash, org_id, role"

def get_user_by_username(username: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text(f"SELECT {_COLS} FROM users WHERE username = :username"),
            {"username": username},
        ).mappings().fetchone()
    return dict(row) if row else None

def get_user_by_id(user_id: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text(f"SELECT {_COLS} FROM users WHERE user_id = :user_id"),
            {"user_id": user_id},
        ).mappings().fetchone()
    return dict(row) if row else None

def username_exists(username: str) -> bool:
    return get_user_by_username(username) is not None

def set_user_org(user_id: str, org_id: str, role: str) -> None:
    with connect() as session:
        session.execute(
            text("UPDATE users SET org_id = :org_id, role = :role WHERE user_id = :user_id"),
            {"org_id": org_id, "role": role, "user_id": user_id},
        )

def list_org_users(org_id: str) -> list[dict]:
    with connect() as session:
        rows = session.execute(
            text("SELECT user_id, username, role FROM users WHERE org_id = :org_id ORDER BY username"),
            {"org_id": org_id},
        ).mappings().fetchall()
    return [dict(r) for r in rows]

def count_admins(org_id: str) -> int:
    with connect() as session:
        count = session.execute(
            text("SELECT COUNT(*) AS c FROM users WHERE org_id = :org_id AND role = 'admin'"),
            {"org_id": org_id},
        ).scalar()
    return count or 0

def delete_user(user_id: str) -> None:
    with connect() as session:
        session.execute(text("DELETE FROM users WHERE user_id = :user_id"), {"user_id": user_id})