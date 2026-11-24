"""Passwords, recovery codes, tokens and a small login rate limiter."""

import os
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer(auto_error=False)


def hash_secret(raw: str) -> str:
    return bcrypt.hashpw(raw.encode()[:72], bcrypt.gensalt(rounds=12)).decode()


def check_secret(raw: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(raw.encode()[:72], hashed.encode())
    except ValueError:
        return False


def new_recovery_code() -> str:
    """12 characters in 3 groups, no look-alike letters: e.g. K7QM-2XRT-9HWD."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_recovery(code: str) -> str:
    c = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{c[:4]}-{c[4:8]}-{c[8:12]}"


def sign_token(user_id: str, token_version: int) -> str:
    now = datetime.now(UTC)
    return jwt.encode({"sub": user_id, "v": token_version, "iat": now, "exp": now + timedelta(days=30)}, settings.jwt_secret, algorithm="HS256")


def read_token(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(401, "Please log in")
    try:
        return jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Your session has ended. Please log in again.")


class RateLimiter:
    """At most `limit` attempts per key in `window` seconds (in memory; fine for one small instance)."""

    def __init__(self, limit: int, window: int):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(429, "Too many attempts. Please wait a few minutes and try again.")
        q.append(now)


login_limiter = RateLimiter(limit=10, window=15 * 60)  # keyed by IP + email, so one person's typos don't block others
# Indian mobile networks put many phones behind one shared IP, so the per-IP sign-up limit is generous.
signup_limiter = RateLimiter(limit=int(os.getenv("SIGNUP_LIMIT_PER_HOUR", "60")), window=60 * 60)
upload_limiter = RateLimiter(limit=20, window=60 * 60)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")
