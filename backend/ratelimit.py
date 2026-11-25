"""
Tiny in-memory sliding-window rate limiter for the auth endpoints
(login / register / password reset), to slow down credential stuffing and
sign-up spam. Single-process only, which matches the planned single-instance
deployment; swap for Redis if the app is ever scaled horizontally.

Configure with env AUTH_RATE_LIMIT="<max requests>/<window seconds>"
(default 10/60, per client IP per endpoint). Set it to "0/60" to disable.
"""
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


def _parse(spec: str):
    try:
        n, w = spec.split("/")
        return int(n), float(w)
    except ValueError:
        return 10, 60.0


class RateLimiter:
    def __init__(self, spec: str = None):
        self.max_hits, self.window = _parse(spec or os.environ.get("AUTH_RATE_LIMIT", "10/60"))
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self._hits.clear()

    def hit(self, key: str):
        if self.max_hits <= 0:
            return
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max_hits:
                retry = int(self.window - (now - q[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail="Too many attempts, please try again later",
                    headers={"Retry-After": str(retry)},
                )
            q.append(now)


auth_limiter = RateLimiter()


def client_ip(request: Request) -> str:
    # Behind a hosting proxy (Render/Railway) the real client is the first X-Forwarded-For hop.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def limit_auth(request: Request):
    """FastAPI dependency: one bucket per (endpoint, client IP)."""
    auth_limiter.hit(f"{request.url.path}|{client_ip(request)}")
