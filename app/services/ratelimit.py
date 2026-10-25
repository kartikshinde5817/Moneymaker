"""A small in-memory limiter for the sign-in, sign-up and email endpoints.

It slows down password guessing and stops the "send me an email" buttons being
used to spam someone. Counts live in this process only, which is right for a
single server; a second server would need a shared store.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def client_ip(request: Request) -> str:
    """The caller's address, looking through the proxy in front of the app."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def hit(key: str, limit: int, per_seconds: int, message: str) -> None:
    """Count one attempt; refuse with 429 once `limit` is passed in the window."""
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and q[0] <= now - per_seconds:
            q.popleft()
        if len(q) >= limit:
            retry = int(q[0] + per_seconds - now) + 1
            raise HTTPException(429, f"{message} Try again in {max(1, retry // 60 + (retry % 60 > 0))} min.",
                                headers={"Retry-After": str(retry)})
        q.append(now)
        if len(_hits) > 20000:  # never let it grow without bound
            for k in [k for k, v in _hits.items() if not v][:5000]:
                _hits.pop(k, None)


def clear(key: str) -> None:
    with _lock:
        _hits.pop(key, None)
