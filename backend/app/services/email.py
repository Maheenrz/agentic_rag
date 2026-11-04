"""
Transactional email for invitations and password resets.
"""

import os
import smtplib
from email.message import EmailMessage

from app.config import APP_ENV, FRONTEND_URL, logger

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# Support the existing SMTP_FROM_EMAIL setting as well.
SMTP_FROM = os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_FROM", "")


def _send(to: str, subject: str, body: str) -> None:
    if APP_ENV != "production":
        # Development-only: invitation/reset URLs contain one-time secrets.
        logger.info(
            "=== EMAIL (dev only) ===\nTo: %s\nSubject: %s\n\n%s\n===================",
            to,
            subject,
            body,
        )
        return

    # Fail closed in production. Never log message bodies containing reset or
    # invitation tokens just because SMTP was accidentally left unconfigured.
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS or not SMTP_FROM:
        logger.error("Production SMTP is not fully configured; email not sent.")
        raise RuntimeError("Email delivery is not configured.")
    if not FRONTEND_URL.startswith("https://"):
        logger.error("Production FRONTEND_URL is not an HTTPS URL; email not sent.")
        raise RuntimeError("Production frontend URL is not configured securely.")

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


def send_invite_email(
    to: str,
    username: str,
    org_name: str,
    token: str,
) -> None:
    link = f"{FRONTEND_URL}/accept-invite#token={token}"

    _send(
        to=to,
        subject=f"You've been invited to {org_name} on Docs Assistant",
        body=(
            f"Hi {username},\n\n"
            f"You've been invited to join {org_name} on Docs Assistant.\n"
            f"Click the link below to set your password. "
            f"It expires in 7 days.\n\n"
            f"{link}\n\n"
            f"If you didn't expect this, ignore this email."
        ),
    )


def send_reset_email(to: str, username: str, token: str) -> None:
    link = f"{FRONTEND_URL}/reset-password#token={token}"

    _send(
        to=to,
        subject="Reset your Docs Assistant password",
        body=(
            f"Hi {username},\n\n"
            "Someone asked to reset your Docs Assistant password. "
            "If it was you, click below.\n"
            "The link expires in 1 hour.\n\n"
            f"{link}\n\n"
            "If this wasn't you, you can safely ignore this email."
        ),
    )


def send_password_changed_email(to: str, username: str) -> None:
    _send(
        to=to,
        subject="Your Docs Assistant password was changed",
        body=(
            f"Hi {username},\n\n"
            "Your password was just changed. If this wasn't you, "
            "reset it immediately and contact your admin.\n"
        ),
    )