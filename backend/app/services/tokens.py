"""
tokens.py
---------
One-time tokens for invites and password resets. Same shape for both:
generate a random URL-safe token, return (plaintext, sha256_hash, expires_at).
Only the hash goes in the DB; only the plaintext goes in the email.
"""
import hashlib
import secrets
import time

INVITE_TTL_SECONDS = 7 * 24 * 60 * 60      # 7 days
RESET_TTL_SECONDS  = 60 * 60               # 1 hour


def new_token(ttl_seconds: int) -> tuple[str, str, float]:
    plaintext = secrets.token_urlsafe(32)
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    expires_at = time.time() + ttl_seconds
    return plaintext, digest, expires_at


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()