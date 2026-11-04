"""
users_store.py
Persistent user accounts managed with SQLAlchemy.
"""

import time
import uuid
from typing import Optional
from sqlalchemy import Float, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import IS_PG, logger
from app.db import Base, engine, connect, table_columns


from app.db import Base, engine, SessionLocal, connect, table_columns, IS_PG



class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Nullable: an invited user exists before they set a password.
    password_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    org_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False, server_default="member")

    # One-time tokens. Stored HASHED; the plaintext only ever exists in the email.
    invite_token_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invite_expires_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reset_token_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reset_expires_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Any JWT with iat < password_changed_at is rejected. Set on password change.
    password_changed_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


def _init_db() -> None:
    if not engine:
        return
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    cols = table_columns("users")
    additions = {
        "org_id":            "TEXT",
        "role":              "TEXT NOT NULL DEFAULT 'member'",
        "email":             "TEXT",
        "invite_token_hash": "TEXT",
        "invite_expires_at": "REAL",
        "reset_token_hash":  "TEXT",
        "reset_expires_at":  "REAL",
        "password_changed_at": "REAL",
    }
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in cols:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
        # Drop NOT NULL on password_hash if it exists (invited users have NULL until activation).
        # Postgres + SQLite syntax differ; handle both.
        try:
            if IS_PG:
                conn.execute(text("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL"))
            # SQLite cannot drop NOT NULL without a table rebuild; skip (SQLite is dev-only).
        except Exception:
            pass


_init_db()

def create_user(username: str, password_hash: Optional[str], org_id: Optional[str] = None,
                role: str = "member", email: Optional[str] = None) -> str:
    user_id = uuid.uuid4().hex
    with SessionLocal() as session:
        u = User(
            user_id=user_id,
            username=username,
            password_hash=password_hash,
            email=email,
            created_at=time.time(),
            org_id=org_id,
            role=role,
        )
        session.add(u)
        session.commit()
    logger.info("Created user: %s (org %s, role %s, email %s)", username, org_id, role, email)
    return user_id

_COLS = "user_id, username, password_hash, email, org_id, role, password_changed_at"


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
            text("SELECT user_id, username, email, role, password_hash IS NOT NULL AS password_set "
                 "FROM users WHERE org_id = :org_id ORDER BY username"),
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


def get_user_by_email(email: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text(f"SELECT {_COLS} FROM users WHERE LOWER(email) = LOWER(:email)"),
            {"email": email},
        ).mappings().fetchone()
    return dict(row) if row else None


def get_user_by_identifier(identifier: str) -> Optional[dict]:
    """Login by username OR email, case-insensitive on email."""
    user = get_user_by_username(identifier)
    if user:
        return user
    if "@" in identifier:
        return get_user_by_email(identifier)
    return None


def email_exists(email: str) -> bool:
    return get_user_by_email(email) is not None


def set_invite_token(user_id: str, token_hash: str, expires_at: float) -> None:
    with connect() as session:
        session.execute(
            text("UPDATE users SET invite_token_hash = :h, invite_expires_at = :e WHERE user_id = :u"),
            {"h": token_hash, "e": expires_at, "u": user_id},
        )


def set_reset_token(user_id: str, token_hash: str, expires_at: float) -> None:
    with connect() as session:
        session.execute(
            text("UPDATE users SET reset_token_hash = :h, reset_expires_at = :e WHERE user_id = :u"),
            {"h": token_hash, "e": expires_at, "u": user_id},
        )


def get_user_by_invite_token_hash(token_hash: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text(f"SELECT {_COLS}, invite_expires_at FROM users WHERE invite_token_hash = :h"),
            {"h": token_hash},
        ).mappings().fetchone()
    return dict(row) if row else None


def get_user_by_reset_token_hash(token_hash: str) -> Optional[dict]:
    with connect() as session:
        row = session.execute(
            text(f"SELECT {_COLS}, reset_expires_at FROM users WHERE reset_token_hash = :h"),
            {"h": token_hash},
        ).mappings().fetchone()
    return dict(row) if row else None


def set_password(user_id: str, password_hash: str) -> None:
    """Used on invite acceptance AND password reset. Bumps password_changed_at so
    every previously-issued JWT becomes invalid."""
    with connect() as session:
        session.execute(
            text("UPDATE users SET password_hash = :h, password_changed_at = :t, "
                 "invite_token_hash = NULL, invite_expires_at = NULL, "
                 "reset_token_hash = NULL, reset_expires_at = NULL "
                 "WHERE user_id = :u"),
            {"h": password_hash, "t": time.time(), "u": user_id},
        )


def get_password_changed_at(user_id: str) -> Optional[float]:
    with connect() as session:
        row = session.execute(
            text("SELECT password_changed_at FROM users WHERE user_id = :u"),
            {"u": user_id},
        ).mappings().fetchone()
    return row["password_changed_at"] if row else None