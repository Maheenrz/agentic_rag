"""
audit_store.py
Append-only audit trail. Answers "who did what, when" for
admin actions, logins, uploads, and security events. log() NEVER raises:
a logging failure must not break the request it is describing.

Privacy: we store the first 200 chars of a flagged question, never full chat
content for ordinary requests.
"""

import json
import time
from typing import Optional
from sqlalchemy import Float, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, engine, SessionLocal

from app.config import logger
from app.db import Base, engine, connect

class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, nullable=False)
    org_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    ip: Mapped[str] = mapped_column(String, nullable=False, server_default="")

    __table_args__ = (
        Index("idx_audit_org_ts", "org_id", "ts"),
    )


def _init_db() -> None:
    if engine:
        Base.metadata.create_all(bind=engine, tables=[AuditLog.__table__])

_init_db()

def log(action: str, user: Optional[dict] = None, target: str = "", detail: Optional[dict] = None,
        ip: str = "", org_id: Optional[str] = None, username: Optional[str] = None) -> None:
    if not SessionLocal:
        return
    try:
        with SessionLocal() as session:
            entry = AuditLog(
                ts=time.time(),
                org_id=org_id or (user or {}).get("org_id"),
                user_id=(user or {}).get("user_id"),
                username=username or (user or {}).get("username"),
                action=action,
                target=target,
                detail_json=json.dumps(detail or {}, default=str)[:4000],
                ip=ip,
            )
            session.add(entry)
            session.commit()
    except Exception:
        logger.exception("audit log write failed for action %s", action)


def list_events(org_id: str, limit: int = 100, action: Optional[str] = None, before_id: Optional[int] = None) -> list[dict]:
    query_str = "SELECT id, ts, user_id, username, action, target, detail_json, ip FROM audit_log WHERE org_id = :org_id"
    params = {"org_id": org_id}

    if action:
        query_str += " AND action = :action"
        params["action"] = action
    if before_id:
        query_str += " AND id < :before_id"
        params["before_id"] = before_id

    query_str += " ORDER BY id DESC LIMIT :limit"
    params["limit"] = max(1, min(limit, 500))

    with connect() as session:
        rows = session.execute(text(query_str), params).mappings().fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
        out.append(d)
    return out