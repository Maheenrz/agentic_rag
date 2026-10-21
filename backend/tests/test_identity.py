
import time

import jwt
import pytest
from fastapi import HTTPException

from app import identity


def test_new_token_is_accepted(monkeypatch):
    monkeypatch.setattr(
        identity,
        "get_user_by_id",
        lambda user_id: {
            "user_id": user_id,
            "password_changed_at": None,
        },
    )

    token = identity.create_access_token("user-123")

    assert identity.get_current_user_id(token=token) == "user-123"


def test_token_issued_before_password_change_is_rejected(monkeypatch):
    token = identity.create_access_token("user-123")

    monkeypatch.setattr(
        identity,
        "get_user_by_id",
        lambda user_id: {
            "user_id": user_id,
            "password_changed_at": time.time() + 1,
        },
    )

    with pytest.raises(HTTPException) as exc:
        identity.get_current_user_id(token=token)

    assert exc.value.status_code == 401


def test_token_issued_after_password_change_is_accepted(monkeypatch):
    # The password change occurred before this token was issued.
    changed_at = time.time() - 10
    token = identity.create_access_token("user-123")

    monkeypatch.setattr(
        identity,
        "get_user_by_id",
        lambda user_id: {
            "user_id": user_id,
            "password_changed_at": changed_at,
        },
    )

    assert identity.get_current_user_id(token=token) == "user-123"


def test_legacy_token_without_iat_is_rejected(monkeypatch):
    token = jwt.encode(
        {
            "sub": "user-123",
            "exp": time.time() + 600,
        },
        identity.JWT_SECRET_KEY,
        algorithm=identity.JWT_ALGORITHM,
    )

    monkeypatch.setattr(
        identity,
        "get_user_by_id",
        lambda user_id: {
            "user_id": user_id,
            "password_changed_at": None,
        },
    )

    with pytest.raises(HTTPException) as exc:
        identity.get_current_user_id(token=token)

    assert exc.value.status_code == 401


def test_deleted_user_token_is_rejected(monkeypatch):
    monkeypatch.setattr(identity, "get_user_by_id", lambda user_id: None)

    token = identity.create_access_token("deleted-user")

    with pytest.raises(HTTPException) as exc:
        identity.get_current_user_id(token=token)

    assert exc.value.status_code == 401
