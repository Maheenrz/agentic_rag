"""
routers/me.py
-------------
Just /me. Kept separate from org.py because it's about the caller, not the org.
"""
from fastapi import APIRouter, Depends

from app.deps import get_current_user
from app.stores import org_store

router = APIRouter(tags=["me"])


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    org = org_store.get_org(user["org_id"]) if user.get("org_id") else None
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "email": user.get("email"),
        "role": user.get("role", "member"),
        "org_id": user.get("org_id"),
        "org": {"org_id": org["org_id"], "name": org["name"]} if org else None,
    }