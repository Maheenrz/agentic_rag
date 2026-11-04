"""
deps.py
-------
Shared FastAPI dependencies, auth helpers, and small utilities used by
multiple routers. Keeping them here means routers stay short and there's
exactly one place that knows how to turn a JWT into a user dict.
"""
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from app.identity import get_current_user_id
from app.guardrails.security import RateLimiter
from app.stores import audit_store, org_store
from app.stores.session_store import thread_belongs_to_user
from app.stores.users_store import get_user_by_id, set_user_org
from app.config import (
    CHAT_LIMIT, FEEDBACK_LIMIT, GUEST_QUESTION_LIMIT, GUEST_UPLOAD_LIMIT,
    LOGIN_FAIL_LIMIT, UPLOAD_LIMIT,
)


# -------- rate limiters (one per category, shared across routers) --------
login_fail_limiter   = RateLimiter(*LOGIN_FAIL_LIMIT)
chat_limiter         = RateLimiter(*CHAT_LIMIT)
upload_limiter       = RateLimiter(*UPLOAD_LIMIT)
feedback_limiter     = RateLimiter(*FEEDBACK_LIMIT)
guest_upload_limiter = RateLimiter(*GUEST_UPLOAD_LIMIT)
guest_question_limiter = RateLimiter(*GUEST_QUESTION_LIMIT)


def _ip(request: Request) -> str:
    # Behind a reverse proxy, configure the proxy to pass X-Forwarded-For and read it here.
    return request.client.host if request.client else ""


def throttle(limiter: RateLimiter, key: str) -> None:
    wait = limiter.retry_after(key)
    if wait:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests. Try again in {wait}s.",
            headers={"Retry-After": str(wait)},
        )
    limiter.hit(key)


# -------- current user --------
def get_current_user(user_id: str = Depends(get_current_user_id)) -> dict:
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
    if not user["org_id"]:
        # Legacy user created before orgs existed: give them a personal org once.
        org_id = org_store.create_org(f"{user['username']}'s organization")
        set_user_org(user_id, org_id, "admin")
        user = get_user_by_id(user_id)
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


# -------- shared lookups --------
def collection_or_404(collection_id: str, user: dict) -> dict:
    """404 (not 403) for anything the user can't read, so ids can't be probed."""
    coll = org_store.get_collection(collection_id, user["org_id"])
    if not coll or not org_store.can_read(user, collection_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    return coll


def require_owned_thread(thread_id: str, user_id: str) -> None:
    if not thread_belongs_to_user(thread_id, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")


def member_of_org(user_id: str, org_id: str) -> bool:
    u = get_user_by_id(user_id)
    return bool(u and u["org_id"] == org_id)


# -------- access snapshot / purge helpers (used by org router) --------
def access_snapshot(org_id: str) -> dict[str, set]:
    from app.stores.users_store import list_org_users
    return {
        u["user_id"]: set(org_store.accessible_collection_ids(
            {"user_id": u["user_id"], "org_id": org_id, "role": u["role"]}
        ))
        for u in list_org_users(org_id)
    }


def purge_after_access_loss(org_id: str, before: dict[str, set]) -> dict:
    from app.rag.document_processor import purge_user_summaries
    after = access_snapshot(org_id)
    purged = {}
    for uid, was in before.items():
        lost = was - after.get(uid, set())
        if lost:
            purged[uid] = purge_user_summaries(uid, lost)
    return purged