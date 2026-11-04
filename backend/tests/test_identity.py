import time
from datetime import datetime, timezone

import jwt
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from app.identity import (
    ACCESS_COOKIE_NAME,
    create_access_token,
    get_current_user_id,
    hash_password,
    verify_password,
)
from app.config import JWT_SECRET_KEY, JWT_ALGORITHM


def _app():
    app = FastAPI()

    @app.get("/protected")
    def protected(user_id: str = Depends(get_current_user_id)):
        return {"user_id": user_id}

    return app


def test_password_hash_roundtrip():
    password = "StrongPassword123!"
    hashed = hash_password(password)

    assert hashed != password
    assert verify_password(password, hashed)
    assert not verify_password("WrongPassword123!", hashed)


def test_password_hash_rejects_more_than_72_utf8_bytes():
    with pytest.raises(ValueError):
        hash_password("a" * 73)


def test_access_token_contains_auth_time():
    token = create_access_token("user-123")
    payload = jwt.decode(
        token,
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
    )

    assert payload["sub"] == "user-123"
    assert isinstance(payload["auth_time"], (int, float))
    assert payload["exp"] > payload["iat"]


def test_protected_route_rejects_missing_cookie():
    client = TestClient(_app())

    response = client.get("/protected")

    assert response.status_code == 401


def test_protected_route_rejects_expired_token(monkeypatch):
    client = TestClient(_app())

    expired_token = jwt.encode(
        {
            "sub": "user-123",
            "iat": int(time.time()) - 3600,
            "auth_time": time.time() - 3600,
            "exp": int(time.time()) - 60,
        },
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )

    client.cookies.set(ACCESS_COOKIE_NAME, expired_token)

    response = client.get("/protected")

    assert response.status_code == 401


def test_protected_route_rejects_legacy_token_without_auth_time(monkeypatch):
    client = TestClient(_app())

    legacy_token = jwt.encode(
        {
            "sub": "user-123",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )

    client.cookies.set(ACCESS_COOKIE_NAME, legacy_token)

    response = client.get("/protected")

    assert response.status_code == 401