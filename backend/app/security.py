"""Password hashing and self-managed JWT issue/verify."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer(auto_error=False)

# bcrypt hard-truncates at 72 bytes; reject longer input instead of silently
# verifying only a prefix (which would make two different passwords equivalent).
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Password is too long (max 72 bytes).")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: UUID, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UUID:
    """Resolve the authenticated tenant id from the Bearer token.

    Deliberately DB-free so `database.py` can depend on it without a cycle.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(credentials.credentials)
        return UUID(str(payload["sub"]))
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
