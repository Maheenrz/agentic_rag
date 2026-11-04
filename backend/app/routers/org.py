"""
routers/org.py
--------------
Everything an org admin does:
  - users:  list / invite / resend invite / delete
  - groups: list / create / delete / add member / remove member
  - audit:  read the audit log
  - feedback: read thumbs-up/down + summary + export
  - guardrails: read / update the org's policy
"""
from typing import Optional
from sqlalchemy.exc import IntegrityError
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.deps import (
    _ip, access_snapshot, get_current_user, member_of_org,
    purge_after_access_loss, require_admin,
)
from app.guardrails import guardrail_policy
from app.models import (
    AclRequest, CollectionRequest, CreateUserRequest, GroupMemberRequest, GroupRequest,
)
from app.services.email import send_invite_email
from app.services.tokens import INVITE_TTL_SECONDS, new_token
from app.stores import audit_store, feedback_store, org_store
from app.stores.session_store import (
    delete_thread, get_message, get_question_for_answer, list_all_thread_ids,
)
from app.stores.users_store import (
    count_admins, create_user, delete_user, get_user_by_id, list_org_users,
    set_invite_token,
)
from app.rag.document_processor import delete_thread_cache, delete_thread_summary
from app.services import cleanup as cleanup_service
from app.core.graph import delete_thread_state

router = APIRouter(prefix="/org", tags=["org"])

_RATINGS = {"up": 1, "down": -1}


# ------------------------------------------------------------------- users
@router.get("/users")
def org_users(admin: dict = Depends(require_admin)):
    return list_org_users(admin["org_id"])


@router.post("/users", status_code=status.HTTP_201_CREATED)
def org_create_user(body: CreateUserRequest, request: Request, admin: dict = Depends(require_admin)):
    """Invite a user. No password in the request: they set their own via email link."""
    email = body.email.lower().strip()

    from app.stores.users_store import email_exists, username_exists
    if username_exists(body.username):
        raise HTTPException(status_code=400, detail="Username already taken")
    if email_exists(email):
        raise HTTPException(status_code=400, detail="An account with that email already exists")

    # Create the user with NO password hash: they can't log in until they accept the invite.
    user_id = create_user(
        body.username, password_hash=None,
        org_id=admin["org_id"], role=body.role, email=email,
    )

    plaintext, digest, expires_at = new_token(INVITE_TTL_SECONDS)
    set_invite_token(user_id, digest, expires_at)

    org = org_store.get_org(admin["org_id"])
    send_invite_email(
        to=email,
        username=body.username,
        org_name=org["name"] if org else "your organisation",
        token=plaintext,
    )

    audit_store.log("user_invited", admin, target=user_id, ip=_ip(request),
                    detail={"username": body.username, "email": email, "role": body.role})

    # Return the token so the admin console can display the invite link even in dev
    # (no email service configured). In prod you may want to omit this.
    return {"user_id": user_id, "invite_token": plaintext}


@router.post("/users/{user_id}/invite", status_code=status.HTTP_200_OK)
def org_resend_invite(user_id: str, request: Request, admin: dict = Depends(require_admin)):
    """Regenerate the invite link for a user who hasn't accepted yet."""
    target = get_user_by_id(user_id)
    if not target or target["org_id"] != admin["org_id"]:
        raise HTTPException(status_code=404, detail="User not found")
    if target.get("password_hash"):
        raise HTTPException(status_code=400, detail="This user has already set a password")

    plaintext, digest, expires_at = new_token(INVITE_TTL_SECONDS)
    set_invite_token(user_id, digest, expires_at)

    org = org_store.get_org(admin["org_id"])
    if target.get("email"):
        send_invite_email(
            to=target["email"], username=target["username"],
            org_name=org["name"] if org else "your organisation",
            token=plaintext,
        )
    audit_store.log("user_invite_resent", admin, target=user_id, ip=_ip(request))
    return {"user_id": user_id, "invite_token": plaintext}


@router.delete("/users/{user_id}")
def org_delete_user(user_id: str, request: Request, admin: dict = Depends(require_admin)):
    target = get_user_by_id(user_id)
    if not target or target["org_id"] != admin["org_id"]:
        raise HTTPException(status_code=404, detail="User not found")
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    if target["role"] == "admin" and count_admins(admin["org_id"]) <= 1:
        raise HTTPException(status_code=400, detail="Can't delete the last admin")

    # Every thread this user owns: messages, semantic cache, memory summary, checkpoint state.
    for thread_id in list_all_thread_ids(user_id):
        delete_thread(thread_id)
        org_store.delete_thread_collections(thread_id)
        feedback_store.delete_thread_feedback(thread_id)
        for cleanup in (delete_thread_cache, delete_thread_summary, delete_thread_state):
            try:
                cleanup(thread_id)
            except Exception:
                from app.config import logger
                logger.exception("Cleanup %s failed for thread %s", cleanup.__name__, thread_id)

    # Their personal collection, if they ever uploaded anything privately.
    # This goes through the same durable cleanup path as an explicit
    # collection delete (app.services.cleanup) -- not a separate ad hoc
    # delete+cascade -- so it gets the same queuing, retry, and
    # verification guarantees instead of being a second, less reliable
    # way to delete a collection's data. If queuing fails here, the user
    # deletion itself is aborted: better to leave the user in place than
    # to delete their account while their private documents' vectors are
    # left behind with no durable record of the unfinished cleanup.
    personal = org_store.get_personal_collection(target)
    if personal:
        try:
            cleanup_service.request_collection_deletion(
                personal["collection_id"], admin["org_id"],
            )
        except Exception as exc:
            from app.config import logger
            logger.exception(
                "Could not queue cleanup for %s's personal collection; "
                "user deletion aborted", user_id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Could not start cleanup of this user's private "
                    "documents. Please retry."
                ),
            ) from exc

    org_store.remove_user_everywhere(user_id)
    delete_user(user_id)

    audit_store.log("user_deleted", admin, target=user_id, ip=_ip(request),
                    detail={"username": target["username"]})
    return {"message": "Deleted"}


# ------------------------------------------------------------------ groups
@router.get("/groups")
def org_groups(admin: dict = Depends(require_admin)):
    return org_store.list_groups(admin["org_id"])



@router.post("/groups", status_code=status.HTTP_201_CREATED)
def org_create_group(
    body: GroupRequest,
    request: Request,
    admin: dict = Depends(require_admin),
):
    try:
        group_id = org_store.create_group(admin["org_id"], body.name)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A group with that name already exists.",
        ) from exc

    audit_store.log(
        "group_created",
        admin,
        target=group_id,
        ip=_ip(request),
        detail={"name": body.name},
    )
    return {"group_id": group_id}


@router.delete("/groups/{group_id}")
def org_delete_group(group_id: str, request: Request, admin: dict = Depends(require_admin)):
    if not org_store.group_in_org(group_id, admin["org_id"]):
        raise HTTPException(status_code=404, detail="Group not found")
    before = access_snapshot(admin["org_id"])
    org_store.delete_group(group_id)
    purged = purge_after_access_loss(admin["org_id"], before)
    audit_store.log("group_deleted", admin, target=group_id, ip=_ip(request),
                    detail={"summaries_purged": purged})
    return {"message": "Deleted"}


@router.post("/groups/{group_id}/members")
def org_add_member(group_id: str, body: GroupMemberRequest, request: Request,
                   admin: dict = Depends(require_admin)):
    if not org_store.group_in_org(group_id, admin["org_id"]) or not member_of_org(body.user_id, admin["org_id"]):
        raise HTTPException(status_code=404, detail="Group or user not found")
    org_store.add_group_member(group_id, body.user_id)
    audit_store.log("group_member_added", admin, target=group_id, ip=_ip(request),
                    detail={"user_id": body.user_id})
    return {"message": "Added"}


@router.delete("/groups/{group_id}/members/{user_id}")
def org_remove_member(group_id: str, user_id: str, request: Request,
                      admin: dict = Depends(require_admin)):
    if not org_store.group_in_org(group_id, admin["org_id"]):
        raise HTTPException(status_code=404, detail="Group not found")
    before = access_snapshot(admin["org_id"])
    org_store.remove_group_member(group_id, user_id)
    purged = purge_after_access_loss(admin["org_id"], before)
    audit_store.log("group_member_removed", admin, target=group_id, ip=_ip(request),
                    detail={"user_id": user_id, "summaries_purged": purged})
    return {"message": "Removed"}


# ------------------------------------------------------------------- audit
@router.get("/audit")
def org_audit(limit: int = 100, action: Optional[str] = None, before_id: Optional[int] = None,
              admin: dict = Depends(require_admin)):
    """Newest first. Page backwards with before_id = the smallest id you already have."""
    return audit_store.list_events(admin["org_id"], limit=limit, action=action, before_id=before_id)


# ---------------------------------------------------------------- feedback
def _private_collection_ids(org_id: str, sources: list[dict]) -> bool:
    """True if any source came from somebody's PERSONAL collection (admins must not read those)."""
    for cid in {s.get("collection_id") for s in sources if s.get("collection_id")}:
        coll = org_store.get_collection(cid, org_id)
        if coll is None or coll["owner_user_id"]:
            return True
    return False


def _feedback_rows(org_id: str, rating: Optional[int], limit: int, before_id: Optional[int]) -> list[dict]:
    users = {u["user_id"]: u["username"] for u in list_org_users(org_id)}
    rows = []
    for fb in feedback_store.list_feedback(org_id, rating, limit, before_id):
        msg = get_message(fb["thread_id"], fb["message_id"])
        if not msg:
            continue
        hidden = _private_collection_ids(org_id, msg["sources"])
        rows.append({
            "id": fb["id"],
            "rating": "up" if fb["rating"] == 1 else "down",
            "reason": fb["reason"],
            "comment": fb["comment"],
            "created_at": fb["created_at"],
            "username": users.get(fb["user_id"], "(deleted)"),
            "question": "[hidden: answer used a personal collection]" if hidden
                        else get_question_for_answer(fb["thread_id"], fb["message_id"]),
            "answer": "[hidden: answer used a personal collection]" if hidden else msg["content"],
            "sources": [] if hidden else [
                {"source": s.get("source"), "location": s.get("location"), "quote": s.get("quote")}
                for s in msg["sources"]
            ],
            "trace": [] if hidden else msg["trace"],
        })
    return rows


@router.get("/feedback")
def org_feedback(rating: Optional[str] = None, limit: int = 50, before_id: Optional[int] = None,
                 admin: dict = Depends(require_admin)):
    """Newest first. rating=down shows only the answers people were unhappy with."""
    return _feedback_rows(admin["org_id"], _RATINGS.get(rating or ""), limit, before_id)


@router.get("/feedback/summary")
def org_feedback_summary(admin: dict = Depends(require_admin)):
    return feedback_store.summary(admin["org_id"])


@router.get("/feedback/export")
def org_feedback_export(admin: dict = Depends(require_admin)):
    """Thumbs-down answers shaped like eval test cases."""
    out = []
    for r in _feedback_rows(admin["org_id"], -1, 200, None):
        if r["question"].startswith("[hidden"):
            continue
        out.append({
            "question": r["question"], "bad_answer": r["answer"], "reason": r["reason"],
            "comment": r["comment"], "cited": r["sources"],
            "gold_answer": "", "gold_pages": [],
        })
    return out


# ---------------------------------------------------------------- guardrails
@router.get("/guardrails")
def get_guardrails(admin: dict = Depends(require_admin)):
    return guardrail_policy.get_policy(admin["org_id"])


@router.put("/guardrails")
def put_guardrails(body: dict, request: Request, admin: dict = Depends(require_admin)):
    """Partial update, e.g. {"pii": {"email": "mask"}, "blocked_topics": ["legal advice"]}."""
    try:
        policy = guardrail_policy.update_policy(admin["org_id"], body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_store.log("guardrails_changed", admin, ip=_ip(request), detail={"update": body})
    return policy