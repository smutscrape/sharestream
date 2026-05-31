"""Authentication primitives: password hashing context, admin JWT issuing /
verification, request client-IP resolution, and the in-memory admin-login
brute-force lockout.

The unlock-cookie helpers and page/media access gating live in
``services.access`` (which imports :data:`pwd_context` from here); this module is
strictly about admin auth and shared crypto primitives.
"""
from __future__ import annotations

import datetime
import logging
import time
from datetime import timedelta, timezone
from threading import Lock

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from sharestream.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    ALGORITHM,
    SECRET_KEY,
)

logger = logging.getLogger(__name__)

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

try:
    HASHED_ADMIN_PASSWORD = pwd_context.hash(ADMIN_PASSWORD)
    logger.info("Admin password hashed successfully")
except Exception as e:  # pragma: no cover - fatal at startup
    logger.error(f"Failed to hash admin password: {e}")
    raise


# ------------------------------------------------------------------
# JWT authentication
# ------------------------------------------------------------------
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None or username != ADMIN_USERNAME:
            raise credentials_exception
        return username
    except JWTError:
        raise credentials_exception


def client_ip(request: Request) -> str:
    """Best-effort real client IP, honoring Cloudflare / proxy headers."""
    if request is None:
        return "unknown"
    h = request.headers
    fwd = h.get("x-forwarded-for", "")
    return (h.get("cf-connecting-ip")
            or (fwd.split(",")[0].strip() if fwd else "")
            or (request.client.host if request.client else "unknown"))


# ------------------------------------------------------------------
# Admin login brute-force protection (per-IP, in-memory)
# ------------------------------------------------------------------
# NOTE: this state is per-process. When the app is eventually run with multiple
# workers this lockout will be per-worker; moving it to a shared store is part
# of the planned multi-worker hardening, not this refactor.
LOGIN_MAX_FAILS = 10
LOGIN_BAN_SECONDS = 24 * 60 * 60
_login_attempts: dict[str, dict] = {}  # ip -> {"fails": int, "banned_until": float}
_login_lock = Lock()


def login_seconds_remaining(ip: str) -> int:
    with _login_lock:
        rec = _login_attempts.get(ip)
        if rec and rec.get("banned_until", 0) > time.time():
            return int(rec["banned_until"] - time.time())
    return 0


def login_record_failure(ip: str) -> None:
    with _login_lock:
        rec = _login_attempts.setdefault(ip, {"fails": 0, "banned_until": 0})
        rec["fails"] += 1
        if rec["fails"] >= LOGIN_MAX_FAILS:
            rec["banned_until"] = time.time() + LOGIN_BAN_SECONDS
            logger.warning(f"Admin login: IP {ip} banned for {LOGIN_BAN_SECONDS}s after {rec['fails']} failures")


def login_record_success(ip: str) -> None:
    with _login_lock:
        _login_attempts.pop(ip, None)
