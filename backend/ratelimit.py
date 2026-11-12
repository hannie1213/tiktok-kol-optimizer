"""
Request throttling — a small in-memory sliding-window limiter.

Protects the endpoints that are cheap to call but expensive to serve or abuse:
sign-in (password guessing), registration and the one-click demo (each one
provisions a seeded tenant), the operator key, and the CPU/LLM-heavy routes.

Counters live in process memory, so with N uvicorn workers the effective
ceiling is up to N × the configured limit — fine for abuse protection, which
is all this is for (billing quotas are enforced separately in the database).
Put a shared store here if you ever need exact cross-worker limits.

Client IPs come from request.client, which uvicorn fills from
X-Forwarded-For when the proxy is trusted (FORWARDED_ALLOW_IPS).
"""
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from backend import settings

_lock = threading.Lock()
_hits: dict = {}
_last_sweep = 0.0


def _sweep(now: float):
    """Drop idle keys so the table can't grow without bound."""
    global _last_sweep
    if now - _last_sweep < 300:
        return
    _last_sweep = now
    for key in [k for k, (q, window) in _hits.items() if not q or now - q[-1] > window]:
        _hits.pop(key, None)


def hit(bucket: str, key: str, limit: int, window: float) -> bool:
    """Record one event; False when `key` already used `limit` events in the
    last `window` seconds (the event is then not recorded)."""
    if not settings.RATE_LIMIT_ENABLED:
        return True
    now = time.monotonic()
    with _lock:
        _sweep(now)
        q, _ = _hits.setdefault((bucket, key), (deque(), window))
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def enforce(bucket: str, key: str, limit: int, window: float,
            detail: str = "Too many requests — please slow down and try again shortly."):
    if not hit(bucket, key, limit, window):
        raise HTTPException(status_code=429, detail=detail,
                            headers={"Retry-After": str(int(window))})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def reset():
    """Forget every counter (tests)."""
    with _lock:
        _hits.clear()
