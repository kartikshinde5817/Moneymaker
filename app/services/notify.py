"""Telegram alerts.

Used for the things worth interrupting you: a prospect replied, a campaign
failed, the daily run finished. Everything else stays in the dashboard.

Set up, once:
  1. Message @BotFather on Telegram, send /newbot, copy the token.
  2. Message your new bot once (it cannot message you first).
  3. Open https://api.telegram.org/bot<TOKEN>/getUpdates and copy
     result[0].message.chat.id
  4. Put both in .env as TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

The /api/notify/test endpoint and the Settings page both verify it.
"""

from __future__ import annotations

import html
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


def ready() -> bool:
    return settings.telegram_ready


def send(text: str, *, preview: bool = False) -> tuple[bool, str]:
    """Send a Telegram message. Never raises - alerts must not break a run."""
    if not ready():
        return False, "Telegram is not configured"
    try:
        r = httpx.post(
            API.format(token=settings.telegram_bot_token, method="sendMessage"),
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": not preview,
            },
            timeout=20,
        )
        if r.status_code != 200:
            detail = r.json().get("description", r.text[:160]) if r.text else r.text
            log.warning("telegram send failed: %s", detail)
            return False, str(detail)[:200]
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram error: %s", exc)
        return False, f"{type(exc).__name__}: {exc}"


def _esc(value: str | None) -> str:
    return html.escape(str(value or ""), quote=False)


def reply_alert(
    *, company: str, contact: str | None, from_email: str, subject: str,
    snippet: str, sentiment: str, lead_url: str, service: str = "",
    score: int | None = None,
) -> tuple[bool, str]:
    """The alert that matters: somebody answered."""
    mood = {
        "interested": "🟢 INTERESTED",
        "not_interested": "🔴 Not interested",
        "unsubscribe": "⛔ Wants to be removed",
        "auto_reply": "⚪ Auto-reply",
        "question": "🟡 Asked a question",
    }.get(sentiment, "📩 Replied")

    lines = [
        f"<b>{mood}</b>",
        "",
        f"<b>{_esc(company)}</b>" + (f"  ·  {score}/100" if score is not None else ""),
    ]
    if contact:
        lines.append(f"From: {_esc(contact)} &lt;{_esc(from_email)}&gt;")
    else:
        lines.append(f"From: {_esc(from_email)}")
    if service:
        lines.append(f"Pitch: {_esc(service.replace('_', ' '))}")
    lines += [
        "",
        f"<b>Re:</b> {_esc(subject)}",
        "",
        f"<blockquote>{_esc(snippet[:600])}</blockquote>",
        "",
        f'<a href="{_esc(lead_url)}">Open the lead</a>',
    ]
    return send("\n".join(lines), preview=False)


def run_summary(*, campaign: str, stats: dict, drafted: int, sent: int, failed: int) -> tuple[bool, str]:
    lines = [
        f"<b>✅ {_esc(campaign)}</b> finished",
        "",
        f"Found {stats.get('found', 0)} · "
        f"{stats.get('leads_created', 0)} leads · "
        f"{drafted} drafted · {sent} sent",
    ]
    if failed:
        lines.append(f"⚠️ {failed} failed to send")
    if stats.get("ai_cost_usd"):
        lines.append(f"AI spend: ${stats['ai_cost_usd']:.3f}")
    return send("\n".join(lines))


def failure_alert(campaign: str, error: str) -> tuple[bool, str]:
    return send(
        f"<b>❌ {_esc(campaign)} failed</b>\n\n<code>{_esc(error[:600])}</code>"
    )
