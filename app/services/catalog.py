"""The list of services an agent can sell.

Four are built in and keep their tuned search phrases in code. Anything the
user adds in the dashboard lives in the `services` table; this module is the
one place the rest of the app asks "what is this service?", so a custom
service works in discovery, scoring and the email writer without each of those
files knowing where it came from.

Reads are cached for a short time because the pipeline asks per prospect.
Writes call `invalidate()` so a new service is usable immediately.
"""

from __future__ import annotations

import re
import threading
import time

from sqlalchemy import select

from app.db import session_scope
from app.models import Service

BUILTIN: dict[str, str] = {
    "web_development": "Web Development",
    "computer_vision": "Computer Vision / OpenCV",
    "ai_agent": "AI Agents",
    "ai_automation": "AI Automation",
    "digital_marketing": "Digital Marketing",
}

KEY_MAX = 32  # width of the service_category columns on leads and campaigns
_TTL = 30.0

_lock = threading.Lock()
_cache: dict | None = None
_loaded_at = 0.0


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def _custom() -> dict[str, dict]:
    global _cache, _loaded_at
    with _lock:
        if _cache is not None and time.monotonic() - _loaded_at < _TTL:
            return _cache
    with session_scope() as db:
        rows = db.execute(select(Service).order_by(Service.id)).scalars().all()
        loaded = {r.key: _row(r) for r in rows}
    with _lock:
        _cache, _loaded_at = loaded, time.monotonic()
    return loaded


def _row(r: Service) -> dict:
    return {
        "key": r.key, "label": r.label, "pitch": r.pitch,
        "keywords": list(r.keywords or []), "intent_phrases": list(r.intent_phrases or []),
        "industries": list(r.industries or []), "builtin": False,
    }


def custom_services() -> dict[str, dict]:
    return dict(_custom())


def all_services() -> list[dict]:
    """Built-in first, then custom in the order they were added."""
    built = [{"key": k, "label": v, "builtin": True} for k, v in BUILTIN.items()]
    return built + list(_custom().values())


def exists(key: str) -> bool:
    return key in BUILTIN or key in _custom()


def label_for(key: str) -> str:
    return BUILTIN.get(key) or _custom().get(key, {}).get("label") or key.replace("_", " ").title()


# ---- what each part of the pipeline reads for a custom service ---------------

def pitch_for(key: str) -> str | None:
    return (_custom().get(key) or {}).get("pitch")


def keywords_for(key: str) -> list[str]:
    return list((_custom().get(key) or {}).get("keywords") or [])


def industries_for(key: str) -> list[str]:
    return list((_custom().get(key) or {}).get("industries") or [])


def intent_phrases_for(key: str) -> list[str]:
    """Quoted phrases for the web and LinkedIn searches.

    Uses the phrases the user wrote. When there are none, builds "looking for X"
    and "need X" from the first keywords, which is the pattern the built-in
    services use.
    """
    svc = _custom().get(key)
    if not svc:
        return []
    phrases = [_quote(p) for p in svc["intent_phrases"]]
    if phrases:
        return phrases
    out: list[str] = []
    for kw in svc["keywords"][:3]:
        out += [f'"looking for {kw}"', f'"need {kw}"']
    return out


def _quote(phrase: str) -> str:
    p = phrase.strip()
    return p if '"' in p else f'"{p}"'


def slugify(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:KEY_MAX] or "service"
    key, n = base, 2
    while key in taken or key in BUILTIN:
        suffix = f"_{n}"
        key = base[: KEY_MAX - len(suffix)] + suffix
        n += 1
    return key
