
"""
identity.py
-----------
Password hashing and JWT issuing/verification.

Tokens issued before a password change are rejected by checking
the JWT issue time against the user's password_changed_at timestamp.
"""

import math
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext

from app.config import JWT_ALGORITHM, JWT_EXPIRE_MINUTES, JWT_SECRET_KEY
from app.stores.users_store import get_user_by_id


_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(plain_password: str) -> str:
    return _pwd_context.hash(plain_password[:72])


def verify_password(plain_password: str, password_hash: str) -> bool:
    return _pwd_context.verify(plain_password, password_hash)


def create_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    issued_at = now.timestamp()

    expire = now + timedelta(minutes=JWT_EXPIRE_MINUTES)

    payload = {
        "sub": user_id,
        "iat": issued_at,
        "exp": expire,
    }

    return jwt.encode(
        payload,
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )


def get_current_user_id(
    token: str = Depends(_oauth2_scheme),
) -> str:
    """
    Validate the JWT and ensure it was issued after the user's
    most recent password change.

    Use this dependency on every endpoint requiring authentication.
    """

    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
        )

        user_id = payload.get("sub")
        issued_at = payload.get("iat")

        # Reject missing or malformed identity/time claims.
        if (
            not isinstance(user_id, str)
            or not user_id.strip()
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
            or not math.isfinite(issued_at)
        ):
            raise credentials_error

        # Legacy tokens without iat are deliberately rejected.
        # All newly issued tokens must have this claim.
        if issued_at > time.time():
            raise credentials_error

    except jwt.InvalidTokenError:
        raise credentials_error

    # Fetch current account state instead of trusting stale JWT data.
    user = get_user_by_id(user_id)

    if not user:
        raise credentials_error

    password_changed_at = user.get("password_changed_at")

    if password_changed_at is not None:
        try:
            changed_at = float(password_changed_at)
        except (TypeError, ValueError):
            raise credentials_error

        if not math.isfinite(changed_at):
            raise credentials_error

        # Reject tokens issued before the latest password change.
        if issued_at < changed_at:
            raise credentials_error

    return user_id
