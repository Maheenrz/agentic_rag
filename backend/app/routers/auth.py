"""
routers/auth.py
---------------
HTTP endpoints for authentication and account recovery.

  POST /auth/register         — create a new org + admin account
  POST /auth/login            — username OR email + password → JWT
  POST /auth/forgot-password  — request a reset link (always 200)
  GET  /auth/reset/{token}    — validate a reset link, return the user info
  POST /auth/reset-password   — set a new password using the reset token
  GET  /auth/invite/{token}   — validate an invite link, return the user info
  POST /auth/accept-invite    — set a password using the invite token
"""
import time
import re

from fastapi import APIRouter, HTTPException, Request, status

from app.identity import create_access_token, hash_password, verify_password
from app.deps import _ip, login_fail_limiter, throttle
from app.models import (
    AcceptInviteRequest, ForgotPasswordRequest, LoginRequest,
    RegisterRequest, ResetPasswordRequest,
)
from app.services.email import send_invite_email, send_reset_email, send_password_changed_email
from app.services.tokens import RESET_TTL_SECONDS, hash_token, new_token
from app.stores import audit_store, org_store
from app.stores.users_store import (
    create_user, email_exists, get_user_by_identifier, get_user_by_invite_token_hash,
    get_user_by_reset_token_hash, set_password, set_reset_token, username_exists,
)

router = APIRouter(prefix="/auth", tags=["auth"])

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_BYTES = 72
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.match(username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be 3-64 characters: letters, digits, _ . @ -",
        )


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {MIN_PASSWORD_LEN} characters",
        )
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at most {MAX_PASSWORD_BYTES} bytes",
        )


# --------------------------------------------------------------------- register
@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request):
    _validate_username(body.username)
    _validate_password(body.password)

    email = body.email.lower().strip()
    if username_exists(body.username):
        raise HTTPException(status_code=400, detail="Username already taken")
    if email_exists(email):
        raise HTTPException(status_code=400, detail="An account with that email already exists")

    org_name = (body.org_name or f"{body.username}'s organization").strip()[:100]
    org_id = org_store.create_org(org_name)
    user_id = create_user(
        body.username, hash_password(body.password),
        org_id=org_id, role="admin", email=email,
    )
    audit_store.log("org_created", target=org_id, ip=_ip(request), org_id=org_id,
                    username=body.username, detail={"user_id": user_id})
    return {"message": "User created"}


# ------------------------------------------------------------------------ login
@router.post("/login")
def login(body: LoginRequest, request: Request):
    """identifier = username OR email."""
    ip = _ip(request)
    key = f"{ip}:{body.identifier.lower()}"

    wait = login_fail_limiter.retry_after(key)
    if wait:
        audit_store.log("login_locked_out", ip=ip, username=body.identifier)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed logins. Try again in {wait}s.",
            headers={"Retry-After": str(wait)},
        )

    user = get_user_by_identifier(body.identifier)
    # Same error message either way: never reveal whether the identifier exists.
    bad = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username/email or password",
    )

    if not user or not user.get("password_hash"):
        login_fail_limiter.hit(key)
        audit_store.log("login_failed", ip=ip, username=body.identifier,
                        org_id=(user or {}).get("org_id"))
        raise bad

    if not verify_password(body.password, user["password_hash"]):
        login_fail_limiter.hit(key)
        audit_store.log("login_failed", ip=ip, username=body.identifier, org_id=user["org_id"])
        raise bad

    login_fail_limiter.clear(key)
    audit_store.log("login_ok", user=user, ip=ip)
    return {
        "access_token": create_access_token(user_id=user["user_id"]),
        "token_type": "bearer",
    }


# --------------------------------------------------------------- forgot / reset
@router.post("/forgot-password", status_code=status.HTTP_200_OK)
def forgot_password(body: ForgotPasswordRequest, request: Request):
    """Always returns 200, whether or not the email exists. Never reveal
    account existence to an unauthenticated caller."""
    ip = _ip(request)
    # Reuse the login limiter, namespaced by "forgot:", to prevent enumeration.
    throttle(login_fail_limiter, f"forgot:{ip}")

    user = get_user_by_identifier(body.email)
    if user and user.get("email"):
        plaintext, digest, expires_at = new_token(RESET_TTL_SECONDS)
        set_reset_token(user["user_id"], digest, expires_at)
        send_reset_email(to=user["email"], username=user["username"], token=plaintext)
        audit_store.log("password_reset_requested", user=user, ip=ip)
    else:
        audit_store.log("password_reset_requested_unknown_email", ip=ip, username=body.email)

    return {"message": "If an account exists for that email, we've sent a reset link."}


@router.get("/reset/{token}")
def lookup_reset(token: str):
    user = get_user_by_reset_token_hash(hash_token(token))
    if not user or (user.get("reset_expires_at") or 0) < time.time():
        raise HTTPException(status_code=404, detail="This reset link is invalid or has expired.")
    return {"username": user["username"], "email": user["email"]}


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, request: Request):
    _validate_password(body.password)
    user = get_user_by_reset_token_hash(hash_token(body.token))
    if not user or (user.get("reset_expires_at") or 0) < time.time():
        raise HTTPException(status_code=404, detail="This reset link is invalid or has expired.")

    set_password(user["user_id"], hash_password(body.password))
    audit_store.log("password_reset_completed", user=user, ip=_ip(request))
    if user.get("email"):
        send_password_changed_email(to=user["email"], username=user["username"])
    return {"message": "Password updated. Please sign in."}


# --------------------------------------------------------------- invite accept
@router.get("/invite/{token}")
def lookup_invite(token: str):
    user = get_user_by_invite_token_hash(hash_token(token))
    if not user or (user.get("invite_expires_at") or 0) < time.time():
        raise HTTPException(status_code=404, detail="This invite link is invalid or has expired.")
    org = org_store.get_org(user["org_id"]) if user.get("org_id") else None
    return {
        "username": user["username"],
        "email": user["email"],
        "org": org["name"] if org else None,
    }


@router.post("/accept-invite")
def accept_invite(body: AcceptInviteRequest, request: Request):
    _validate_password(body.password)
    user = get_user_by_invite_token_hash(hash_token(body.token))
    if not user or (user.get("invite_expires_at") or 0) < time.time():
        raise HTTPException(status_code=404, detail="This invite link is invalid or has expired.")

    set_password(user["user_id"], hash_password(body.password))
    audit_store.log("invite_accepted", user=user, ip=_ip(request))
    return {"message": "Password set. You can sign in now."}