"""
Password hashing and JWT authentication using an HttpOnly cookie.

Keep the existing bcrypt scheme so previously stored password hashes remain
verifiable. Password length is validated in auth.py in UTF-8 bytes (max 72).
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, Request, status
from passlib.context import CryptContext

from app.config import JWT_ALGORITHM, JWT_EXPIRE_MINUTES, JWT_SECRET_KEY
from app.stores.users_store import get_user_by_id

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ACCESS_COOKIE_NAME = "access_token"


def hash_password(plain_password: str) -> str:
    # auth.py must reject passwords longer than 72 UTF-8 bytes before calling.
    encoded = plain_password.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("Password must be at most 72 UTF-8 bytes.")
    return _pwd_context.hash(encoded)


def verify_password(plain_password: str, password_hash: str) -> bool:
    encoded = plain_password.encode("utf-8")
    if len(encoded) > 72:
        return False
    return _pwd_context.verify(encoded, password_hash)


def create_access_token(user_id: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    now = datetime.now(timezone.utc).timestamp()
    payload = {
        "sub": str(user_id),
        "iat": int(now),
        # Float timestamp avoids second-level rounding around password changes.
        "auth_time": now,
        "exp": expires_at,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_current_user_id(request: Request) -> str:
    """FastAPI dependency: authenticate from the HttpOnly access_token cookie."""
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    if not token:
        raise credentials_error

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        auth_time = payload.get("auth_time")

        # Tokens issued before this claim was introduced are deliberately
        # rejected, so deploying this change also clears legacy sessions.
        if not user_id or not isinstance(auth_time, (int, float)):
            raise credentials_error

        user = get_user_by_id(str(user_id))
        if not user:
            raise credentials_error

        changed_at = user.get("password_changed_at")
        if changed_at is not None and float(auth_time) < float(changed_at):
            raise credentials_error

        return str(user_id)

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except jwt.InvalidTokenError:
        raise credentials_error