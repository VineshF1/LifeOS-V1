"""Neon / PostgreSQL async engine, session factory, RLS tenant scoping, audit trail.

Tenant isolation is enforced in three independent layers:

1. Every request runs inside an explicit transaction with `app.current_user_id`
   pinned via `set_config(..., is_local => true)`, so the value can never leak
   across pooled connections.
2. Row-Level Security policies on `documents`, `document_chunks`, `tasks` and
   `audit_logs` read that setting. Tables are created with FORCE ROW LEVEL
   SECURITY (see schema.sql) because a table's owner otherwise bypasses RLS --
   and on Neon the application connects as the owner.
3. Every vector / list query additionally carries an explicit
   `WHERE user_id = ...` predicate (defence in depth).
"""
from __future__ import annotations

import json
import logging
import ssl
from collections.abc import AsyncIterator, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .security import get_current_user_id

# libpq-only query parameters that asyncpg's connect() does not understand.
_STRIPPED_PARAMS = {
    "channel_binding",
    "target_session_attrs",
    "options",
    "connect_timeout",
    "application_name",
    "gssencmode",
    "sslcert",
    "sslkey",
    "sslrootcert",
}


def normalize_database_url(raw: str) -> tuple[str, dict]:
    """Return an asyncpg-compatible URL plus the matching connect_args.

    Neon hands out `postgresql://...?sslmode=require&channel_binding=require`,
    neither of which asyncpg accepts verbatim.
    """
    raw = (raw or "").strip()
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    if raw.startswith("postgresql://"):
        raw = "postgresql+asyncpg://" + raw[len("postgresql://") :]

    parts = urlsplit(raw)
    query = dict(parse_qsl(parts.query))
    sslmode = query.pop("sslmode", None)
    for key in _STRIPPED_PARAMS:
        query.pop(key, None)

    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    connect_args: dict = {
        # Neon's pooled endpoint is PgBouncer in transaction mode, which cannot
        # serve prepared statements. Disabling asyncpg's statement cache is the
        # documented fix and is harmless on a direct connection too.
        "statement_cache_size": 0,
        "server_settings": {"application_name": "lifeos-agent"},
    }

    host = (parts.hostname or "").lower()
    is_local = host in ("", "localhost", "127.0.0.1", "::1")

    if sslmode in ("disable", "allow"):
        connect_args["ssl"] = False
    elif sslmode in ("verify-ca", "verify-full"):
        connect_args["ssl"] = ssl.create_default_context()
    elif sslmode is not None or not is_local:
        # libpq `require` semantics: encrypt the transport, do not reject on
        # certificate verification (Neon's cert chain isn't always in the
        # Windows trust store).
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ctx

    return url, connect_args


_engine_url, _engine_connect_args = normalize_database_url(settings.DATABASE_URL)

engine = create_async_engine(
    _engine_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=_engine_connect_args,
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

_SET_TENANT_SQL = text("SELECT set_config('app.current_user_id', :user_id, true)")


def to_vector_literal(values: Sequence[float]) -> str:
    """Render an embedding as a pgvector literal, e.g. '[0.1,-0.2,...]'.

    Passed as a text parameter and cast in SQL (`::text::vector(2048)`), which
    keeps asyncpg free of any custom pgvector type codec.
    """
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


async def set_tenant_context(session: AsyncSession, user_id: UUID) -> None:
    """Pin the RLS tenant for the current transaction only (is_local => true)."""
    await session.execute(_SET_TENANT_SQL, {"user_id": str(user_id)})


async def get_session() -> AsyncIterator[AsyncSession]:
    """Unauthenticated session (signup / login only).

    No tenant GUC is set, so every RLS-protected table returns zero rows and
    rejects writes -- the fail-closed default.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            yield session


async def get_tenant_session(
    user_id: UUID = Depends(get_current_user_id),
) -> AsyncIterator[AsyncSession]:
    """Request-scoped transaction with the authenticated tenant pinned."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await set_tenant_context(session, user_id)
            yield session


async def record_audit(
    session: AsyncSession,
    user_id: UUID,
    action_type: str,
    *,
    tool_name: str | None = None,
    input_payload: dict | None = None,
    output_payload: dict | None = None,
    execution_time_ms: int | None = None,
) -> None:
    """Append an observability row. Never raises into the caller's request path.

    Wrapped in a SAVEPOINT because PostgreSQL aborts the whole transaction on a
    failed statement -- without it, a rejected audit row would poison every
    subsequent query in the request.
    """
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    """
                    INSERT INTO audit_logs
                        (user_id, action_type, tool_name, input_payload,
                         output_payload, execution_time_ms)
                    VALUES
                        (CAST(CAST(:user_id AS text) AS uuid), :action_type, :tool_name,
                         CAST(CAST(:input_payload AS text) AS jsonb),
                         CAST(CAST(:output_payload AS text) AS jsonb),
                         :execution_time_ms)
                    """
                ),
                {
                    "user_id": str(user_id),
                    "action_type": action_type,
                    "tool_name": tool_name,
                    "input_payload": _json_or_none(input_payload),
                    "output_payload": _json_or_none(output_payload),
                    "execution_time_ms": execution_time_ms,
                },
            )
    except Exception:  # pragma: no cover - observability must never break a request
        logging.getLogger("lifeos.audit").exception("Failed to write audit log")


def _json_or_none(payload: dict | None) -> str | None:
    if payload is None:
        return None
    try:
        return json.dumps(payload, default=str)[:20000]
    except (TypeError, ValueError):
        return None


def placeholders(values: Iterable[object], prefix: str = "p") -> tuple[str, dict]:
    """Build a safe `(:p0, :p1, ...)` fragment plus its bind params."""
    params: dict[str, object] = {}
    names: list[str] = []
    for index, value in enumerate(values):
        key = f"{prefix}{index}"
        names.append(f":{key}")
        params[key] = value
    return ", ".join(names), params
