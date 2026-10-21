"""
email.py
--------
Sending transactional email. In dev, logs the full message (including the
action link) so you can test the whole flow without SMTP.

To go live: set APP_ENV=production and fill in SMTP_* in .env, then swap the
body of _send() for a real sendmail/SES/SendGrid call. The templates and the
call sites don't change.
"""
import os
import smtplib
from email.message import EmailMessage

from app.config import APP_ENV, logger

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "no-reply@docs-assistant.local")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")


def _send(to: str, subject: str, body: str) -> None:
    if APP_ENV != "production" or not SMTP_HOST:
        # Dev path: print to console so you can copy the link.
        logger.info("=== EMAIL (dev) ===\nTo: %s\nSubject: %s\n\n%s\n===================", to, subject, body)
        return

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


def send_invite_email(to: str, username: str, org_name: str, token: str) -> None:
    link = f"{FRONTEND_URL}/accept-invite?token={token}"
    _send(
        to=to,
        subject=f"You've been invited to {org_name} on Docs Assistant",
        body=(
            f"Hi {username},\n\n"
            f"You've been invited to join {org_name} on Docs Assistant.\n"
            f"Click the link below to set your password. It expires in 7 days.\n\n"
            f"{link}\n\n"
            f"If you didn't expect this, ignore this email."
        ),
    )


def send_reset_email(to: str, username: str, token: str) -> None:
    link = f"{FRONTEND_URL}/reset-password?token={token}"
    _send(
        to=to,
        subject="Reset your Docs Assistant password",
        body=(
            f"Hi {username},\n\n"
            f"Someone asked to reset your Docs Assistant password. If it was you, click below.\n"
            f"The link expires in 1 hour.\n\n"
            f"{link}\n\n"
            f"If this wasn't you, you can safely ignore this email."
        ),
    )


def send_password_changed_email(to: str, username: str) -> None:
    _send(
        to=to,
        subject="Your Docs Assistant password was changed",
        body=(
            f"Hi {username},\n\n"
            f"Your password was just changed. If this wasn't you, reset it immediately "
            f"and contact your admin.\n"
        ),
    )