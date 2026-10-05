"""
main.py
-------
FastAPI backend: auth + orgs + collections with access control + chat.

Who can do what (enforced HERE, server-side, never by the frontend):
  - anyone can sign up; signing up creates a NEW org and makes you its admin
    (it never lets you join an existing org)
  - admins create users, groups, collections, set who can read each
    collection, and upload/delete documents in org collections
  - members can read only the collections they were granted (directly or via a
    group) plus any collection marked visibility='org'
  - every member also has a private "personal" collection that only they see

Retrieval safety: chat never trusts the client about which documents to
search. It asks org_store which collections THIS user may read, optionally
narrows that list by what the client requested (can only shrink it), and
hands Chroma a filter built from that list.
"""
import json
import re
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from app.stores import audit_store
from app.stores import feedback_store
from app.guardrails import guardrail_policy
from app.stores import org_store
from app.auth import create_access_token, get_current_user_id, hash_password, verify_password
from app.config import (CHAT_LIMIT, CORS_ORIGINS, DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, FEEDBACK_LIMIT, LOGIN_FAIL_LIMIT,
                    MAX_UPLOAD_MB, RERANK_CANDIDATE_K, UPLOAD_LIMIT, logger)

from app.rag.document_processor import (
    build_org_retriever,
    delete_collection_chunks,
    delete_doc_chunks,
    delete_thread_cache,
    delete_thread_summary,
    get_embedding_mode_label,
    purge_summaries_for_document,
    purge_user_summaries,
    save_thread_summary,
)
from app.stores.session_store import (
    add_message, create_thread, delete_thread, get_message, get_messages, get_question_for_answer,
    list_all_thread_ids,
    list_threads, rename_thread, thread_belongs_to_user,
)
from app.stores.users_store import (
    count_admins, create_user, delete_user,
    get_user_by_id, get_user_by_username, list_org_users, set_user_org, username_exists,
)

from app.core.graph import compiled_graph, delete_thread_state, small_llm, summarize_thread_for_storage
from app.rag.ingest import SuspiciousDocument, ingest_file
import re as _re
from app.guardrails.guard_llm import classify_input
from app.guardrails.pii import apply_policy as apply_pii_policy
from app.guardrails.security import POLICY_BLOCK_MESSAGE, RateLimiter, check_user_input

MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_BYTES = 72                      # bcrypt silently ignores/rejects anything beyond this
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")

login_fail_limiter = RateLimiter(*LOGIN_FAIL_LIMIT)
chat_limiter = RateLimiter(*CHAT_LIMIT)
upload_limiter = RateLimiter(*UPLOAD_LIMIT)
feedback_limiter = RateLimiter(*FEEDBACK_LIMIT)


def _ip(request: Request) -> str:
    # Behind a reverse proxy this is the proxy's address; configure the proxy to
    # pass X-Forwarded-For and read it here when we get to docker-compose.
    return request.client.host if request.client else ""


def _throttle(limiter: RateLimiter, key: str) -> None:
    """Counts one call and rejects with 429 when over the limit."""
    wait = limiter.retry_after(key)
    if wait:
        raise HTTPException(status_code=429, detail=f"Too many requests. Try again in {wait}s.",
                            headers={"Retry-After": str(wait)})
    limiter.hit(key)


def _validate_new_credentials(username: str, password: str) -> None:
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="Username must be 3-64 characters: letters, digits, _ . @ -")
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LEN} characters")
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(status_code=400, detail=f"Password must be at most {MAX_PASSWORD_BYTES} bytes")

app = FastAPI(title="Private Docs Assistant API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://frontendagentic-n5bnld82y-mahheen508-gmailcoms-projects.vercel.app", # Your exact Vercel URL
        "http://localhost:5173", # For local development
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# ------------------------------------------------------------------ models
class RegisterRequest(BaseModel):
    username: str
    password: str
    org_name: Optional[str] = None


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "member"


class GroupRequest(BaseModel):
    name: str


class GroupMemberRequest(BaseModel):
    user_id: str


class CollectionRequest(BaseModel):
    name: str
    description: str = ""
    visibility: str = "restricted"


class AclRequest(BaseModel):
    user_ids: list[str] = []
    group_ids: list[str] = []
    visibility: Optional[str] = None


class FeedbackRequest(BaseModel):
    rating: str                      # "up" or "down"
    reason: str = ""                 # optional, one of feedback_store.REASONS (useful on "down")
    comment: str = ""                # optional free text


class RenameRequest(BaseModel):
    title: str


class ChatRequest(BaseModel):
    question: str
    force_offline: bool = False      # kept so the old frontend doesn't break; ignored (server decides)
    top_k: int = 4
    collection_ids: Optional[list[str]] = None   # optional narrowing; can never widen access


# -------------------------------------------------------------- dependencies
def get_current_user(user_id: str = Depends(get_current_user_id)) -> dict:
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
    if not user["org_id"]:
        # Account created before orgs existed: give it a personal org once.
        org_id = org_store.create_org(f"{user['username']}'s organization")
        set_user_org(user_id, org_id, "admin")
        user = get_user_by_id(user_id)
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def _collection_or_404(collection_id: str, user: dict) -> dict:
    """404 (not 403) for anything the user can't read, so ids can't be probed."""
    coll = org_store.get_collection(collection_id, user["org_id"])
    if not coll or not org_store.can_read(user, collection_id):
        raise HTTPException(status_code=404, detail="Collection not found")
    return coll


def _access_snapshot(org_id: str) -> dict[str, set]:
    """{user_id: set(collection ids they can read)} for everyone in the org."""
    return {u["user_id"]: set(org_store.accessible_collection_ids({"user_id": u["user_id"], "org_id": org_id, "role": u["role"]}))
            for u in list_org_users(org_id)}


def _purge_after_access_loss(org_id: str, before: dict[str, set]) -> dict:
    """Run AFTER a change that can shrink someone's access. For every user who
    lost a collection, delete the recall summaries built from it."""
    after = _access_snapshot(org_id)
    purged = {}
    for uid, was in before.items():
        lost = was - after.get(uid, set())
        if lost:
            purged[uid] = purge_user_summaries(uid, lost)
    return purged


def _require_owned_thread(thread_id: str, user_id: str) -> None:
    if not thread_belongs_to_user(thread_id, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")


# -------------------------------------------------------------------- auth
@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request):
    _validate_new_credentials(body.username, body.password)
    if username_exists(body.username):
        raise HTTPException(status_code=400, detail="Username already taken")
    org_id = org_store.create_org((body.org_name or f"{body.username}'s organization").strip()[:100])
    user_id = create_user(body.username, hash_password(body.password), org_id=org_id, role="admin")
    audit_store.log("org_created", target=org_id, ip=_ip(request), org_id=org_id, username=body.username,
                    detail={"user_id": user_id})
    return {"message": "User created"}


@app.post("/auth/login")
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    ip = _ip(request)
    key = f"{ip}:{form_data.username.lower()}"
    wait = login_fail_limiter.retry_after(key)          # brute-force lockout: only FAILED attempts count
    if wait:
        audit_store.log("login_locked_out", ip=ip, username=form_data.username)
        raise HTTPException(status_code=429, detail=f"Too many failed logins. Try again in {wait}s.",
                            headers={"Retry-After": str(wait)})
    user = get_user_by_username(form_data.username)
    if not user or not verify_password(form_data.password, user["password_hash"]):
        login_fail_limiter.hit(key)
        audit_store.log("login_failed", ip=ip, username=form_data.username, org_id=(user or {}).get("org_id"))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    login_fail_limiter.clear(key)
    audit_store.log("login_ok", user=user, ip=ip)
    return {"access_token": create_access_token(user_id=user["user_id"]), "token_type": "bearer"}


@app.get("/me")
def me(user: dict = Depends(get_current_user)):
    org = org_store.get_org(user["org_id"])
    return {"user_id": user["user_id"], "username": user["username"], "role": user["role"],
            "org": {"org_id": org["org_id"], "name": org["name"]}}


# ------------------------------------------------------------ org admin
@app.get("/org/users")
def org_users(admin: dict = Depends(require_admin)):
    return list_org_users(admin["org_id"])


@app.post("/org/users", status_code=status.HTTP_201_CREATED)
def org_create_user(body: CreateUserRequest, request: Request, admin: dict = Depends(require_admin)):
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")
    _validate_new_credentials(body.username, body.password)
    if username_exists(body.username):
        raise HTTPException(status_code=400, detail="Username already taken")
    user_id = create_user(body.username, hash_password(body.password), org_id=admin["org_id"], role=body.role)
    audit_store.log("user_created", admin, target=user_id, ip=_ip(request), detail={"username": body.username, "role": body.role})
    return {"user_id": user_id}


@app.get("/org/groups")
def org_groups(admin: dict = Depends(require_admin)):
    return org_store.list_groups(admin["org_id"])


@app.post("/org/groups", status_code=status.HTTP_201_CREATED)
def org_create_group(body: GroupRequest, request: Request, admin: dict = Depends(require_admin)):
    try:
        group_id = org_store.create_group(admin["org_id"], body.name)
    except Exception:
        raise HTTPException(status_code=400, detail="Group name already exists")
    audit_store.log("group_created", admin, target=group_id, ip=_ip(request), detail={"name": body.name})
    return {"group_id": group_id}


@app.delete("/org/groups/{group_id}")
def org_delete_group(group_id: str, request: Request, admin: dict = Depends(require_admin)):
    if not org_store.group_in_org(group_id, admin["org_id"]):
        raise HTTPException(status_code=404, detail="Group not found")
    before = _access_snapshot(admin["org_id"])
    org_store.delete_group(group_id)
    purged = _purge_after_access_loss(admin["org_id"], before)
    audit_store.log("group_deleted", admin, target=group_id, ip=_ip(request), detail={"summaries_purged": purged})
    return {"message": "Deleted"}


def _member_of_org(user_id: str, org_id: str) -> bool:
    u = get_user_by_id(user_id)
    return bool(u and u["org_id"] == org_id)


@app.post("/org/groups/{group_id}/members")
def org_add_member(group_id: str, body: GroupMemberRequest, request: Request, admin: dict = Depends(require_admin)):
    if not org_store.group_in_org(group_id, admin["org_id"]) or not _member_of_org(body.user_id, admin["org_id"]):
        raise HTTPException(status_code=404, detail="Group or user not found")
    org_store.add_group_member(group_id, body.user_id)
    audit_store.log("group_member_added", admin, target=group_id, ip=_ip(request), detail={"user_id": body.user_id})
    return {"message": "Added"}


@app.delete("/org/groups/{group_id}/members/{user_id}")
def org_remove_member(group_id: str, user_id: str, request: Request, admin: dict = Depends(require_admin)):
    if not org_store.group_in_org(group_id, admin["org_id"]):
        raise HTTPException(status_code=404, detail="Group not found")
    before = _access_snapshot(admin["org_id"])
    org_store.remove_group_member(group_id, user_id)
    purged = _purge_after_access_loss(admin["org_id"], before)
    audit_store.log("group_member_removed", admin, target=group_id, ip=_ip(request),
                    detail={"user_id": user_id, "summaries_purged": purged})
    return {"message": "Removed"}


@app.delete("/org/users/{user_id}")
def org_delete_user(user_id: str, request: Request, admin: dict = Depends(require_admin)):
    target = get_user_by_id(user_id)
    if not target or target["org_id"] != admin["org_id"]:
        raise HTTPException(status_code=404, detail="User not found")
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    if target["role"] == "admin" and count_admins(admin["org_id"]) <= 1:
        raise HTTPException(status_code=400, detail="Can't delete the last admin")

    # every thread this user owns: messages, semantic cache, memory summary, checkpoint state
    for thread_id in list_all_thread_ids(user_id):
        delete_thread(thread_id)
        org_store.delete_thread_collections(thread_id)
        feedback_store.delete_thread_feedback(thread_id)
        feedback_store.delete_thread_feedback(thread_id)
        for cleanup in (delete_thread_cache, delete_thread_summary, delete_thread_state):
            try:
                cleanup(thread_id)
            except Exception:
                logger.exception("Cleanup %s failed for thread %s", cleanup.__name__, thread_id)

    # their personal collection, if they ever uploaded anything privately
    personal = org_store.get_personal_collection(target)
    if personal:
        delete_collection_chunks(personal["collection_id"])
        org_store.delete_collection_cascade(personal["collection_id"])

    # group memberships + any ACL entries naming them directly
    org_store.remove_user_everywhere(user_id)

    delete_user(user_id)
    audit_store.log("user_deleted", admin, target=user_id, ip=_ip(request), detail={"username": target["username"]})
    return {"message": "Deleted"}


@app.get("/org/audit")
def org_audit(limit: int = 100, action: Optional[str] = None, before_id: Optional[int] = None,
              admin: dict = Depends(require_admin)):
    """Newest first. Page backwards with before_id = the smallest id you already have."""
    return audit_store.list_events(admin["org_id"], limit=limit, action=action, before_id=before_id)

# ------------------------------------------------------------ collections
@app.get("/collections")
def my_collections(user: dict = Depends(get_current_user)):
    return org_store.list_collections_for(user)


@app.post("/collections", status_code=status.HTTP_201_CREATED)
def create_collection(body: CollectionRequest, request: Request, admin: dict = Depends(require_admin)):
    try:
        cid = org_store.create_collection(admin["org_id"], body.name, body.description, body.visibility)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=400, detail="Collection name already exists")
    audit_store.log("collection_created", admin, target=cid, ip=_ip(request),
                    detail={"name": body.name, "visibility": body.visibility})
    return {"collection_id": cid}


@app.get("/collections/{collection_id}/acl")
def get_acl(collection_id: str, admin: dict = Depends(require_admin)):
    coll = org_store.get_collection(collection_id, admin["org_id"])
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"visibility": coll["visibility"], **org_store.get_collection_acl(collection_id)}


@app.put("/collections/{collection_id}/acl")
def set_acl(collection_id: str, body: AclRequest, request: Request, admin: dict = Depends(require_admin)):
    coll = org_store.get_collection(collection_id, admin["org_id"])
    if not coll or coll["owner_user_id"]:
        raise HTTPException(status_code=404, detail="Collection not found")
    org_user_ids = {u["user_id"] for u in list_org_users(admin["org_id"])}
    if not set(body.user_ids) <= org_user_ids:
        raise HTTPException(status_code=400, detail="Unknown user id")
    if not all(org_store.group_in_org(g, admin["org_id"]) for g in body.group_ids):
        raise HTTPException(status_code=400, detail="Unknown group id")
    before = _access_snapshot(admin["org_id"])
    org_store.set_collection_acl(collection_id, body.user_ids, body.group_ids)
    if body.visibility:
        try:
            org_store.set_visibility(collection_id, body.visibility)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    purged = _purge_after_access_loss(admin["org_id"], before)      # anyone who lost access loses related recall memory
    audit_store.log("acl_changed", admin, target=collection_id, ip=_ip(request), detail={
        "user_ids": body.user_ids, "group_ids": body.group_ids, "visibility": body.visibility, "summaries_purged": purged})
    return {"message": "Access updated"}


# ---------------------------------------------------------------- documents
def _read_upload(f: UploadFile) -> bytes:
    data = f.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{f.filename} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    return data


_OK_STATUSES = ("added", "new_version", "unchanged")


def _ingest_many(user: dict, coll: dict, files: list[UploadFile], chunk_size: int, chunk_overlap: int,
                 allow_flagged: bool, ip: str):
    """Per-file results (one bad file doesn't undo the others). If NOTHING was
    ingested the request fails (400 bad file / 422 suspicious content) with the
    per-file results attached; otherwise 200 and the caller checks each status."""
    results = []
    for f in files:
        name = f.filename or "untitled"
        try:
            data = _read_upload(f)
            res = ingest_file(user, coll, name, data, chunk_size, chunk_overlap, allow_flagged=allow_flagged)
            results.append(res)
            if res["status"] != "unchanged":
                audit_store.log("document_uploaded", user, target=res.get("doc_id", ""), ip=ip, detail={
                    "collection_id": coll["collection_id"], "filename": res["filename"], "version": res["version"],
                    "findings": len(res.get("findings", [])), "notes": res.get("notes", [])})
            if res.get("overridden"):
                audit_store.log("upload_security_override", user, target=res["doc_id"], ip=ip, detail={
                    "filename": res["filename"], "findings": res["findings"]})
        except SuspiciousDocument as exc:
            audit_store.log("upload_blocked_suspicious", user, ip=ip, detail={
                "collection_id": coll["collection_id"], "filename": exc.filename, "findings": exc.findings})
            results.append({"filename": exc.filename, "status": "rejected_suspicious", "error": str(exc),
                            "findings": exc.findings})
        except HTTPException as exc:                       # too large etc.
            results.append({"filename": name, "status": "error", "error": exc.detail})
        except ValueError as exc:
            results.append({"filename": name, "status": "error", "error": str(exc)})
        except RuntimeError as exc:                        # embedding-mode guard
            raise HTTPException(status_code=503, detail=str(exc))
    if not any(r["status"] in _OK_STATUSES for r in results):
        code = 422 if any(r["status"] == "rejected_suspicious" for r in results) else 400
        return JSONResponse(status_code=code, content={"detail": results[0]["error"], "results": results})
    return {"results": results}


def _check_embeddings() -> None:
    try:
        org_store.assert_embedding_mode(get_embedding_mode_label())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/collections/{collection_id}/documents")
def collection_documents(collection_id: str, user: dict = Depends(get_current_user)):
    _collection_or_404(collection_id, user)
    return org_store.list_documents(collection_id)


@app.post("/collections/{collection_id}/documents")
def upload_to_collection(
    collection_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    chunk_size: int = Form(DEFAULT_CHUNK_SIZE),
    chunk_overlap: int = Form(DEFAULT_CHUNK_OVERLAP),
    allow_flagged: bool = Form(False),      # admin-only override for files with HIGH injection findings
    user: dict = Depends(get_current_user),
):
    coll = _collection_or_404(collection_id, user)
    if not org_store.can_write(user, coll):
        raise HTTPException(status_code=403, detail="You can't upload to this collection")
    _throttle(upload_limiter, user["user_id"])
    _check_embeddings()
    return _ingest_many(user, coll, files, chunk_size, chunk_overlap, allow_flagged, _ip(request))


@app.delete("/collections/{collection_id}/documents/{doc_id}")
def delete_document(collection_id: str, doc_id: str, request: Request, user: dict = Depends(get_current_user)):
    coll = _collection_or_404(collection_id, user)
    if not org_store.can_write(user, coll):
        raise HTTPException(status_code=403, detail="You can't delete from this collection")
    doc = org_store.get_document(doc_id, collection_id)
    if not doc or doc["status"] == "deleted":
        raise HTTPException(status_code=404, detail="Document not found")
    delete_doc_chunks(doc_id)
    org_store.set_document_status(doc_id, "deleted")
    org_store.bump_version(collection_id)       # cached answers built on this doc stop matching
    org_user_ids = [u["user_id"] for u in list_org_users(user["org_id"])]
    purged = purge_summaries_for_document(doc_id, collection_id, org_user_ids)   # recall memory built from it
    audit_store.log("document_deleted", user, target=doc_id, ip=_ip(request),
                    detail={"collection_id": collection_id, "filename": doc["filename"], "summaries_purged": purged})
    return {"message": "Deleted"}


# ------------------------------------------------------------------ threads
@app.post("/threads")
def new_thread(user: dict = Depends(get_current_user)):
    return {"thread_id": create_thread(user["user_id"])}


@app.get("/threads")
def my_threads(user: dict = Depends(get_current_user)):
    return list_threads(user["user_id"])


@app.get("/threads/{thread_id}/messages")
def thread_messages(thread_id: str, user: dict = Depends(get_current_user)):
    _require_owned_thread(thread_id, user["user_id"])
    messages = get_messages(thread_id)
    mine = feedback_store.ratings_for_thread(thread_id, user["user_id"])
    for msg in messages:
        if msg["role"] == "assistant":
            msg["feedback"] = mine.get(msg["id"])          # None, or {"rating": 1|-1, "reason", "comment"}
    return messages


@app.patch("/threads/{thread_id}")
def rename(thread_id: str, body: RenameRequest, user: dict = Depends(get_current_user)):
    _require_owned_thread(thread_id, user["user_id"])
    rename_thread(thread_id, body.title)
    return {"message": "Renamed"}


@app.delete("/threads/{thread_id}")
def remove_thread(thread_id: str, user: dict = Depends(get_current_user)):
    _require_owned_thread(thread_id, user["user_id"])
    delete_thread(thread_id)
    org_store.delete_thread_collections(thread_id)
    feedback_store.delete_thread_feedback(thread_id)
    for cleanup in (delete_thread_cache, delete_thread_summary, delete_thread_state):
        try:
            cleanup(thread_id)
        except Exception:
            logger.exception("Cleanup %s failed for thread %s", cleanup.__name__, thread_id)
    return {"message": "Deleted"}


@app.post("/threads/{thread_id}/upload")
def upload_to_personal(
    thread_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    force_offline: bool = Form(False),            # ignored; kept for the existing frontend
    chunk_size: int = Form(DEFAULT_CHUNK_SIZE),
    chunk_overlap: int = Form(DEFAULT_CHUNK_OVERLAP),
    allow_flagged: bool = Form(False),
    user: dict = Depends(get_current_user),
):
    """Old sidebar upload. Files now land in the user's PERSONAL collection
    (visible only to them, in every one of their chats), not in this thread."""
    _require_owned_thread(thread_id, user["user_id"])
    _throttle(upload_limiter, user["user_id"])
    _check_embeddings()
    coll = org_store.get_or_create_personal_collection(user)
    return _ingest_many(user, coll, files, chunk_size, chunk_overlap, allow_flagged, _ip(request))


# --------------------------------------------------------------------- chat
def _prepare_run(thread_id: str, user: dict, body: ChatRequest, policy: dict):
    allowed = org_store.accessible_collection_ids(user)
    if body.collection_ids:
        wanted = set(body.collection_ids)
        allowed = [c for c in allowed if c in wanted]          # intersection: can only shrink

    retriever = None
    if org_store.has_active_documents(allowed):
        _check_embeddings()
        retriever = build_org_retriever(user["org_id"], allowed, k=RERANK_CANDIDATE_K)

    graph_input = {
        "question": body.question, "search_query": "", "retrieved_docs": [], "relevant_docs": [],
        "rewrite_count": 0, "answer": "", "trace": [], "tool_used": "", "sources": [],
        "faithfulness_verdict": "", "regenerated": False, "security_events": [],
    }
    config = {"configurable": {
        "thread_id": thread_id, "user_id": user["user_id"], "retriever": retriever,
        "top_k": body.top_k, "force_offline": False,
        "guardrails": policy,
        # cache key = which collections + their versions + the org's guardrail policy
        "scope": org_store.access_scope(allowed) + ":" + guardrail_policy.fingerprint(policy),
    }}
    return graph_input, config


TOPIC_BLOCK_MESSAGE = "This assistant isn't set up to answer questions about that topic."
PII_QUESTION_BLOCK_MESSAGE = "Please don't include personal or financial identifiers in your question. Remove them and ask again."


def _policy_gate(user: dict, body: ChatRequest, ip: str, policy: dict) -> Optional[tuple[str, str]]:
    """Runs BEFORE the model sees anything. Order: rate limit -> length / prompt-extraction ->
    blocked topics -> PII (mask or block) -> optional model second opinion on suspicious questions.
    Returns None to proceed (body.question may have been rewritten with PII masked), or
    (message_for_the_user, reason_for_the_audit_log) when blocked."""
    _throttle(chat_limiter, user["user_id"])
    allowed, reason, flags = check_user_input(
        body.question, max_chars=policy["max_question_chars"], block_extraction=policy["block_prompt_extraction"])
    if flags:
        audit_store.log("question_flagged", user, ip=ip, detail={
            "rules": [f.rule for f in flags], "question": body.question[:200]})
    if not allowed:
        audit_store.log("question_blocked", user, ip=ip, detail={"reason": reason, "question": body.question[:200]})
        return POLICY_BLOCK_MESSAGE, reason

    lowered = body.question.lower()
    for topic in policy["blocked_topics"]:
        if _re.search(r"(?<!\w)" + _re.escape(topic) + r"(?!\w)", lowered):
            audit_store.log("question_blocked", user, ip=ip, detail={"reason": "blocked topic", "topic": topic})
            return TOPIC_BLOCK_MESSAGE, f"blocked topic: {topic}"

    masked_q, masked, blocked = apply_pii_policy(body.question, policy["pii"])
    if blocked:                                  # log the TYPES only, never the values
        audit_store.log("question_blocked", user, ip=ip, detail={"reason": "pii", "kinds": blocked})
        return PII_QUESTION_BLOCK_MESSAGE, "pii: " + ", ".join(blocked)
    if masked:
        body.question = masked_q                 # from here on (graph, cache, chat history, logs) only the masked text exists
        audit_store.log("pii_masked_in_question", user, ip=ip, detail={"kinds": masked})

    if flags and policy["llm_second_opinion"]:   # cascade: the model is only asked about questions the free rules found suspicious
        verdict = classify_input(body.question, small_llm)
        audit_store.log("llm_guard_verdict", user, ip=ip, detail={"verdict": verdict, "rules": [f.rule for f in flags]})
        if verdict == "ATTACK":
            return POLICY_BLOCK_MESSAGE, "llm guard: attack"
    return None


def _after_chat(thread_id: str, user: dict, state: dict, ip: str) -> None:
    """Remember which collections this thread used (for purge-on-revoke) and
    audit anything the security nodes did."""
    org_store.record_thread_collections(thread_id, [s.get("collection_id") for s in state.get("sources", [])])
    org_store.record_thread_documents(thread_id, [s.get("doc_id") for s in state.get("sources", [])])
    for event in state.get("security_events", []):
        audit_store.log("security_event", user, target=thread_id, ip=ip, detail=event)


@app.post("/threads/{thread_id}/chat")
def chat(thread_id: str, body: ChatRequest, request: Request, user: dict = Depends(get_current_user)):
    _require_owned_thread(thread_id, user["user_id"])
    ip = _ip(request)
    policy = guardrail_policy.get_policy(user["org_id"])
    blocked = _policy_gate(user, body, ip, policy)
    if blocked:
        add_message(thread_id, "user", body.question)
        trace = [f"Policy gate: blocked ({blocked[1]})"]
        message_id = add_message(thread_id, "assistant", blocked[0], trace, [])
        return {"answer": blocked[0], "sources": [], "trace": trace, "message_id": message_id}
    add_message(thread_id, "user", body.question)         # (already PII-masked if the policy says so)

    graph_input, config = _prepare_run(thread_id, user, body, policy)
    final_state = compiled_graph.invoke(graph_input, config=config)
    answer = final_state.get("answer", "")
    sources = final_state.get("sources", [])
    trace = final_state.get("trace", [])
    message_id = add_message(thread_id, "assistant", answer, trace, sources)
    _after_chat(thread_id, user, final_state, ip)
    return {"answer": answer, "sources": sources, "trace": trace, "message_id": message_id}


STREAM_NODES = {"generate", "regenerate"}


@app.post("/threads/{thread_id}/chat/stream")
def chat_stream(thread_id: str, body: ChatRequest, request: Request, user: dict = Depends(get_current_user)):
    _require_owned_thread(thread_id, user["user_id"])
    ip = _ip(request)
    policy = guardrail_policy.get_policy(user["org_id"])
    blocked = _policy_gate(user, body, ip, policy)
    if blocked:
        add_message(thread_id, "user", body.question)
        trace = [f"Policy gate: blocked ({blocked[1]})"]
        message_id = add_message(thread_id, "assistant", blocked[0], trace, [])
        payload = json.dumps({"type": "final", "answer": blocked[0], "sources": [], "trace": trace,
                              "message_id": message_id}) + "\n"
        return StreamingResponse(iter([payload]), media_type="application/x-ndjson")
    add_message(thread_id, "user", body.question)
    graph_input, config = _prepare_run(thread_id, user, body, policy)

    def emit(obj: dict) -> str:
        return json.dumps(obj) + "\n"

    def event_stream():
        state: dict = {}
        try:
            for mode, payload in compiled_graph.stream(
                graph_input, config=config, stream_mode=["updates", "messages"]
            ):
                if mode == "updates":
                    for _node, update in payload.items():
                        if not update:
                            continue
                        state.update(update)
                        if "trace" in update:
                            yield emit({"type": "trace", "trace": update["trace"]})
                        if update.get("faithfulness_verdict") == "UNSUPPORTED":
                            yield emit({"type": "reset"})
                else:
                    chunk, meta = payload
                    if meta.get("langgraph_node") in STREAM_NODES and isinstance(chunk.content, str) and chunk.content:
                        yield emit({"type": "token", "text": chunk.content})

            answer = state.get("answer", "")
            sources = state.get("sources", [])
            trace = state.get("trace", [])
            message_id = add_message(thread_id, "assistant", answer, trace, sources)
            _after_chat(thread_id, user, state, ip)
            # NOTE for the frontend: the output guard can rewrite the answer AFTER tokens
            # were streamed, so always replace the streamed text with this final answer.
            yield emit({"type": "final", "answer": answer, "sources": sources, "trace": trace, "message_id": message_id})
        except Exception as exc:
            logger.exception("Streaming chat failed")
            yield emit({"type": "error", "detail": str(exc)})

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@app.post("/threads/{thread_id}/finalize")
def finalize_thread(thread_id: str, user: dict = Depends(get_current_user)):
    _require_owned_thread(thread_id, user["user_id"])
    snap = compiled_graph.get_state({"configurable": {"thread_id": thread_id}})
    values = snap.values or {}
    title, summary = summarize_thread_for_storage(values.get("raw_turns", []), values.get("running_summary", ""))
    if not summary:
        return {"saved": False}
    save_thread_summary(thread_id, title, summary, user["user_id"],
                        collection_ids=org_store.get_thread_collections(thread_id),
                        doc_ids=org_store.get_thread_documents(thread_id))
    return {"saved": True, "title": title}


# ---------------------------------------------------------------- feedback (thumbs up / down)
_RATINGS = {"up": 1, "down": -1}


@app.put("/threads/{thread_id}/messages/{message_id}/feedback")
def rate_message(thread_id: str, message_id: int, body: FeedbackRequest, user: dict = Depends(get_current_user)):
    """Rate one assistant answer. Calling it again changes your rating."""
    _require_owned_thread(thread_id, user["user_id"])
    if body.rating not in _RATINGS:
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    if body.reason and body.reason not in feedback_store.REASONS:
        raise HTTPException(status_code=400, detail=f"reason must be one of: {', '.join(feedback_store.REASONS)}")
    msg = get_message(thread_id, message_id)
    if not msg or msg["role"] != "assistant":
        raise HTTPException(status_code=404, detail="Answer not found")
    _throttle(feedback_limiter, user["user_id"])
    feedback_store.set_feedback(message_id, thread_id, user["user_id"], user["org_id"], _RATINGS[body.rating],
                                body.reason, body.comment.strip()[:1000])
    return {"message": "Thanks for the feedback"}


@app.delete("/threads/{thread_id}/messages/{message_id}/feedback")
def clear_rating(thread_id: str, message_id: int, user: dict = Depends(get_current_user)):
    _require_owned_thread(thread_id, user["user_id"])
    if not feedback_store.remove_feedback(message_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="No rating to remove")
    return {"message": "Removed"}


def _private_collection_ids(org_id: str, sources: list[dict]) -> bool:
    """True if any source came from somebody's PERSONAL collection (admins must not read those)."""
    for cid in {s.get("collection_id") for s in sources if s.get("collection_id")}:
        coll = org_store.get_collection(cid, org_id)
        if coll is None or coll["owner_user_id"]:
            return True                     # personal, or already deleted: don't expose
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
            "id": fb["id"], "rating": "up" if fb["rating"] == 1 else "down", "reason": fb["reason"],
            "comment": fb["comment"], "created_at": fb["created_at"], "username": users.get(fb["user_id"], "(deleted)"),
            "question": "[hidden: answer used a personal collection]" if hidden else get_question_for_answer(fb["thread_id"], fb["message_id"]),
            "answer": "[hidden: answer used a personal collection]" if hidden else msg["content"],
            "sources": [] if hidden else [{"source": s.get("source"), "location": s.get("location"), "quote": s.get("quote")}
                                          for s in msg["sources"]],
            "trace": [] if hidden else msg["trace"],
        })
    return rows


@app.get("/org/feedback")
def org_feedback(rating: Optional[str] = None, limit: int = 50, before_id: Optional[int] = None,
                 admin: dict = Depends(require_admin)):
    """Newest first. rating=down shows only the answers people were unhappy with."""
    return _feedback_rows(admin["org_id"], _RATINGS.get(rating or ""), limit, before_id)


@app.get("/org/feedback/summary")
def org_feedback_summary(admin: dict = Depends(require_admin)):
    return feedback_store.summary(admin["org_id"])


@app.get("/org/feedback/export")
def org_feedback_export(admin: dict = Depends(require_admin)):
    """Thumbs-down answers shaped like eval test cases: add the correct answer / gold page
    by hand, then run them through run_eval.py."""
    out = []
    for r in _feedback_rows(admin["org_id"], -1, 200, None):
        if r["question"].startswith("[hidden"):
            continue
        out.append({"question": r["question"], "bad_answer": r["answer"], "reason": r["reason"],
                    "comment": r["comment"], "cited": r["sources"], "gold_answer": "", "gold_pages": []})
    return out


# ---------------------------------------------------------------- guardrail policy (per organisation)
@app.get("/org/guardrails")
def get_guardrails(admin: dict = Depends(require_admin)):
    """The effective policy: defaults plus whatever this org's admins changed."""
    return guardrail_policy.get_policy(admin["org_id"])


@app.put("/org/guardrails")
def put_guardrails(body: dict, request: Request, admin: dict = Depends(require_admin)):
    """Partial update, e.g. {"pii": {"email": "mask"}, "blocked_topics": ["legal advice"]}."""
    try:
        policy = guardrail_policy.update_policy(admin["org_id"], body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_store.log("guardrails_changed", admin, ip=_ip(request), detail={"update": body})
    return policy