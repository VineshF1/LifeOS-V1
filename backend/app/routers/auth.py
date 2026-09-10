"""Custom JWT signup and login for the self-managed identity store."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session, get_tenant_session
from ..schemas import LoginRequest, SignupRequest, TokenResponse, UserOut
from ..security import create_access_token, get_current_user_id, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

_SELECT_USER = text("SELECT id, email, created_at FROM users WHERE id = CAST(CAST(:user_id AS text) AS uuid)")

# Verified against on every failed login so the response time does not reveal
# whether the email exists.
_DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.eS7X4vOZ0B0FqQ2u0k1H8h1nXbN5m2u"


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    email = payload.email.strip().lower()
    hashed = hash_password(payload.password)

    try:
        async with session.begin_nested():
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO users (email, hashed_password)
                        VALUES (:email, :hashed_password)
                        RETURNING id, email, created_at
                        """
                    ),
                    {"email": email, "hashed_password": hashed},
                )
            ).mappings().one()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        ) from exc

    user = UserOut(**dict(row))
    return TokenResponse(access_token=create_access_token(user.id, user.email), user=user)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    email = payload.email.strip().lower()
    row = (
        await session.execute(
            text("SELECT id, email, hashed_password, created_at FROM users WHERE email = :email"),
            {"email": email},
        )
    ).mappings().first()

    if row is None:
        verify_password(payload.password, _DUMMY_HASH)  # equalise timing
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password."
        )

    if not verify_password(payload.password, row["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password."
        )

    user = UserOut(id=row["id"], email=row["email"], created_at=row["created_at"])
    return TokenResponse(access_token=create_access_token(user.id, user.email), user=user)


@router.get("/me", response_model=UserOut)
async def me(
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> UserOut:
    row = (await session.execute(_SELECT_USER, {"user_id": str(user_id)})).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return UserOut(**dict(row))
