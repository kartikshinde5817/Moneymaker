"""Email the platform itself sends: verify an address, reset a password,
contact-form messages. These come from the server's own mailbox (SMTP_* in the
environment), never from a customer's mailbox.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import threading
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from app.config import settings

log = logging.getLogger(__name__)


def sender_address() -> str:
    return (settings.sender_email or settings.smtp_user or "").strip()


def ready() -> bool:
    return bool(settings.smtp_host and sender_address())


def verification_enforced() -> bool:
    """Verification is only demanded while we can actually deliver the email."""
    return bool(settings.require_email_verification and ready())


def _send(to: str, subject: str, text: str, html: str | None, reply_to: str | None) -> bool:
    msg = EmailMessage()
    sender = sender_address()
    msg["From"] = f"{settings.platform_name} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host, port = settings.smtp_host, settings.smtp_port
    try:
        if settings.smtp_use_ssl or port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls()
                server.ehlo()
            elif settings.smtp_user:
                raise RuntimeError(f"{host}:{port} cannot encrypt; not sending credentials")
        with server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_pass)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, socket.error, OSError, RuntimeError) as exc:
        log.error("platform email to %s failed: %s: %s", to, type(exc).__name__, exc)
        return False


def send(to: str, subject: str, text: str, html: str | None = None,
         reply_to: str | None = None, *, background: bool = True) -> bool:
    """Send one email. In the background by default so a slow mail server never
    holds up the page; returns whether it was handed off (or sent)."""
    if not ready():
        log.warning("platform email not configured (SMTP_HOST / SENDER_EMAIL); would send %r to %s:\n%s",
                    subject, to, text)
        return False
    if background:
        threading.Thread(target=_send, args=(to, subject, text, html, reply_to), daemon=True).start()
        return True
    return _send(to, subject, text, html, reply_to)


def button_html(intro: str, button: str, url: str, outro: str = "") -> str:
    """A plain, client-safe HTML body with one button and the link as text."""
    name = settings.platform_name
    return f"""<div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;color:#0B1220;line-height:1.55">
<p style="font-size:20px;font-weight:700;margin:0 0 18px">{name}</p>
<p>{intro}</p>
<p style="margin:24px 0"><a href="{url}" style="background:#0E7490;color:#fff;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:600;display:inline-block">{button}</a></p>
<p style="font-size:13px;color:#5D6D7B">Or paste this link into your browser:<br><span style="word-break:break-all">{url}</span></p>
<p style="font-size:13px;color:#5D6D7B">{outro}</p></div>"""
