"""The two emails every account needs: confirm your address, reset your password."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import settings
from app.models import User
from app.services import auth, platform_mail


def _base() -> str:
    return settings.public_base_url.rstrip("/")


def send_verification(db: Session, user: User, background: bool = True) -> bool:
    link = f"{_base()}/auth/verify?token={auth.issue_token(db, user, 'verify')}"
    name = (user.name or "there").split()[0]
    text = (f"Hi {name},\n\nWelcome to {settings.platform_name}. Please confirm your email address "
            f"so you can start your free trial:\n\n{link}\n\nThe link works for 48 hours. "
            f"If you did not create this account, you can ignore this email.\n")
    html = platform_mail.button_html(
        f"Hi {name}, welcome to {settings.platform_name}. Please confirm your email address so you can start your free trial.",
        "Confirm my email", link,
        "The link works for 48 hours. If you did not create this account, you can ignore this email.")
    return platform_mail.send(user.email, f"Confirm your email for {settings.platform_name}", text, html,
                              background=background)


def send_password_reset(db: Session, user: User) -> bool:
    link = f"{_base()}/reset?token={auth.issue_token(db, user, 'reset')}"
    name = (user.name or "there").split()[0]
    text = (f"Hi {name},\n\nSomeone asked to reset the password for your {settings.platform_name} account. "
            f"To choose a new one, open:\n\n{link}\n\nThe link works for 1 hour and only once. "
            f"If this was not you, ignore this email: your password stays as it is.\n")
    html = platform_mail.button_html(
        f"Hi {name}, someone asked to reset the password for your {settings.platform_name} account.",
        "Choose a new password", link,
        "The link works for 1 hour and only once. If this was not you, ignore this email: your password stays as it is.")
    return platform_mail.send(user.email, f"Reset your {settings.platform_name} password", text, html)
