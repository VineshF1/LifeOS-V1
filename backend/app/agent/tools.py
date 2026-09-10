"""Registered agent tools.

Each tool runs inside the caller's tenant-scoped transaction, so RLS applies to
every statement even though the tools also carry an explicit `user_id` filter.
Every invocation is written to `audit_logs` with its duration and payload.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import record_audit, to_vector_literal
from ..ingestion import embed_query
from ..schemas import coerce_iso_date

# `:qv` is always bound as text and cast inside SQL, which keeps asyncpg free of
# any pgvector type codec. Embeds are stored as vector(2048) but every ordered
# index/scan goes through halfvec(2048) -- pgvector caps HNSW at 2000 dims for
# the `vector` type, so the expression index in schema.sql is on the halfvec cast.
_QV = "CAST(CAST(:qv AS text) AS halfvec(2048))"
# Must carry the explicit typmod so it matches the expression index in schema.sql.
_EMB = "CAST(embedding AS halfvec(2048))"
_UV = "CAST(CAST(:user_id AS text) AS uuid)"
_DV = "CAST(CAST(:document_id AS text) AS uuid)"

MAX_TOP_K = 12


@dataclass
class ToolContext:
    """Per-turn state shared by every tool call."""

    session: AsyncSession
    user_id: UUID
    document_id: UUID | None = None
    # chunk_id -> chunk record, for every chunk actually shown to the model.
    # Citation rendering only accepts refs found here.
    seen_chunks: dict[str, dict[str, Any]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Tool 1: search_documents
# --------------------------------------------------------------------------


async def search_documents(ctx: ToolContext, query: str, top_k: int = 4) -> dict[str, Any]:
    """Vector similarity retrieval over the authenticated user's chunks."""
    if not isinstance(query, str) or not query.strip():
        return {"error": "`query` must be a non-empty string."}

    try:
        limit = max(1, min(int(top_k), MAX_TOP_K))
    except (TypeError, ValueError):
        limit = 4

    vector = await embed_query(query.strip())
    literal = to_vector_literal(vector)

    params: dict[str, Any] = {"qv": literal, "user_id": str(ctx.user_id), "top_k": limit}
    filters = [f"user_id = {_UV}"]
    if ctx.document_id:
        filters.append(f"document_id = {_DV}")
        params["document_id"] = str(ctx.document_id)

    where_clause = " AND ".join(filters)
    rows = (
        await ctx.session.execute(
            text(
                f"""
                SELECT id, document_id, filename, page_number, content,
                       1 - ({_QV} <=> {_EMB}) AS score
                FROM document_chunks
                WHERE {where_clause}
                ORDER BY {_QV} <=> {_EMB}
                LIMIT :top_k
                """
            ),
            params,
        )
    ).mappings().all()

    chunks: list[dict[str, Any]] = []
    for row in rows:
        chunk_id = str(row["id"])
        chunk = {
            "chunk_id": chunk_id,
            "document_id": str(row["document_id"]),
            "filename": row["filename"],
            "page_number": int(row["page_number"] or 1),
            "content": row["content"],
            "score": round(float(row["score"]), 4),
        }
        chunks.append(chunk)
        # Register for citation verification (last write wins is fine; content is identical).
        ctx.seen_chunks[chunk_id] = chunk

    return {
        "query": query,
        "scoped_to_document": str(ctx.document_id) if ctx.document_id else None,
        "result_count": len(chunks),
        "chunks": chunks,
    }


# --------------------------------------------------------------------------
# Tool 2: query_structured_data
# --------------------------------------------------------------------------


async def query_structured_data(
    ctx: ToolContext,
    category: str | None = None,
    expiring_before: str | None = None,
    issuer: str | None = None,
) -> dict[str, Any]:
    """Exact SQL filtering over extracted metadata (no vector search)."""
    params: dict[str, Any] = {"user_id": str(ctx.user_id)}
    conditions = [f"user_id = {_UV}"]

    if category and str(category).strip():
        conditions.append("category = :category")
        params["category"] = str(category).strip().title()

    if issuer and str(issuer).strip():
        conditions.append("metadata->>'issuer' ILIKE :issuer")
        params["issuer"] = f"%{str(issuer).strip()}%"

    if expiring_before and str(expiring_before).strip():
        deadline = coerce_iso_date(expiring_before)
        if deadline is None:
            return {
                "error": "`expiring_before` must be a date (YYYY-MM-DD).",
                "result_count": 0,
                "documents": [],
            }
        # Stored deadlines are normalised ISO dates, so text comparison is a
        # correct chronological comparison.
        conditions.append("metadata->>'action_deadline' <= :expiring_before")
        conditions.append("metadata->>'action_deadline' IS NOT NULL")
        params["expiring_before"] = deadline

    if ctx.document_id:
        conditions.append(f"id = {_DV}")
        params["document_id"] = str(ctx.document_id)

    rows = (
        await ctx.session.execute(
            text(
                f"""
                SELECT id, filename, category, status, has_actionable_deadline,
                       metadata->>'issuer'             AS issuer,
                       metadata->>'identifier'         AS identifier,
                       metadata->>'action_deadline'    AS action_deadline,
                       metadata->>'action_description' AS action_description,
                       metadata->>'financial_amount'   AS financial_amount,
                       metadata->>'currency'           AS currency
                FROM documents
                WHERE {' AND '.join(conditions)}
                ORDER BY metadata->>'action_deadline' ASC NULLS LAST, created_at DESC
                LIMIT 25
                """
            ),
            params,
        )
    ).mappings().all()

    documents = [dict(row) for row in rows]
    for document in documents:
        document["id"] = str(document["id"])

    return {
        "filters": {
            "category": category,
            "expiring_before": expiring_before,
            "issuer": issuer,
        },
        "result_count": len(documents),
        "documents": documents,
    }


# --------------------------------------------------------------------------
# Tool 3: create_task
# --------------------------------------------------------------------------


async def create_task(
    ctx: ToolContext,
    title: str,
    due_date: str | None = None,
    source_document_id: str | None = None,
) -> dict[str, Any]:
    """Insert a pending task owned by the authenticated tenant."""
    if not isinstance(title, str) or not title.strip():
        return {"error": "`title` must be a non-empty string."}

    resolved_document_id = source_document_id or ctx.document_id
    document_id: str | None = None

    if resolved_document_id:
        try:
            candidate = UUID(str(resolved_document_id))
        except (ValueError, AttributeError, TypeError):
            return {"error": f"`source_document_id` is not a valid UUID: {resolved_document_id!r}"}

        # RLS restricts this lookup to the caller's own documents.
        exists = await ctx.session.scalar(
            text(
                f"""SELECT 1 FROM documents
                    WHERE id = CAST(CAST(:doc_id AS text) AS uuid) AND user_id = {_UV}"""
            ),
            {"doc_id": str(candidate), "user_id": str(ctx.user_id)},
        )
        if exists:
            document_id = str(candidate)
        else:
            # Never create a task with silently-dropped provenance: an id that
            # does not resolve to one of the caller's documents is a
            # hallucination, and the model should know that.
            return {"error": "`source_document_id` does not belong to any of your documents."}

    normalized_due = coerce_iso_date(due_date)
    if due_date and normalized_due is None:
        return {"error": f"`due_date` must be a date (YYYY-MM-DD), received {due_date!r}."}

    row = (
        await ctx.session.execute(
            text(
                """
                INSERT INTO tasks (user_id, document_id, title, due_date, status)
                VALUES (
                    CAST(CAST(:user_id AS text) AS uuid),
                    CAST(CAST(:document_id AS text) AS uuid),
                    :title,
                    CAST(CAST(:due_date AS text) AS date),
                    'pending'
                )
                RETURNING id, title, due_date, status, document_id
                """
            ),
            {
                "user_id": str(ctx.user_id),
                "document_id": document_id,
                "title": title.strip()[:255],
                "due_date": normalized_due,
            },
        )
    ).mappings().one()

    return {
        "created": True,
        "task_id": str(row["id"]),
        "title": row["title"],
        "due_date": row["due_date"].isoformat() if row["due_date"] else None,
        "status": row["status"],
        "source_document_id": str(row["document_id"]) if row["document_id"] else None,
    }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

ToolHandler = Callable[..., Coroutine[Any, Any, dict[str, Any]]]

TOOL_REGISTRY: dict[str, ToolHandler] = {
    "search_documents": search_documents,
    "query_structured_data": query_structured_data,
    "create_task": create_task,
}

CATEGORIES = ["Insurance", "Tax", "Vehicle", "Utility", "Warranty", "Rental", "General"]

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Semantic search over the text of the user's stored documents. "
                "Returns the most relevant chunks with their chunk_id, filename, page "
                "number and content. Use this when the answer depends on what a "
                "document actually says (clauses, coverage, terms, wording)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of chunks to return. Default 4.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_structured_data",
            "description": (
                "Exact SQL filtering over the structured fields already extracted from "
                "the user's documents: category, issuer, and action deadline. Use this "
                "for filtering or aggregation questions such as 'which policies expire "
                "next month' or 'list my utility bills'. It does not read document text, "
                "so it is faster and exact where it applies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": CATEGORIES,
                        "description": "Filter by document category. Omit for all categories.",
                    },
                    "expiring_before": {
                        "type": "string",
                        "description": (
                            "ISO date YYYY-MM-DD. Returns only documents whose action "
                            "deadline falls on or before this date. Omit to ignore deadlines."
                        ),
                    },
                    "issuer": {
                        "type": "string",
                        "description": (
                            "Case-insensitive partial match on the issuer/provider name. "
                            "Omit to ignore the issuer."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Create an actionable task for the user. Call this only when the user "
                "asks you to add, schedule, or track something, or explicitly asks for a "
                "reminder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short imperative task title, e.g. 'Pay electricity bill'.",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD the task is due.",
                    },
                    "source_document_id": {
                        "type": "string",
                        "description": (
                            "UUID of the document this task came from. Use the document_id "
                            "given in the conversation scope."
                        ),
                    },
                },
                "required": ["title", "due_date", "source_document_id"],
            },
        },
    },
]


async def execute_tool(
    ctx: ToolContext, name: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Dispatch one tool call, audit it, and return (result, duration_ms).

    Tool failures are returned to the model as data rather than raised, so the
    agent loop can adapt instead of collapsing the turn.
    """
    handler = TOOL_REGISTRY.get(name)
    started = time.perf_counter()

    if handler is None:
        result: dict[str, Any] = {"error": f"Unknown tool '{name}'."}
    else:
        try:
            # SAVEPOINT: a handler that fails mid-statement would otherwise abort
            # the request transaction and break every later query.
            async with ctx.session.begin_nested():
                result = await handler(ctx, **arguments)
        except TypeError as exc:
            result = {"error": f"Invalid arguments for '{name}': {exc}"}
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as data
            result = {"error": f"'{name}' failed: {exc}"}

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    await record_audit(
        ctx.session,
        ctx.user_id,
        "tool_call",
        tool_name=name,
        input_payload=arguments,
        output_payload=_audit_payload(result),
        execution_time_ms=elapsed_ms,
    )
    return result, elapsed_ms


def _audit_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Keep audit rows small: full chunk text is already in document_chunks."""
    if "chunks" in result:
        trimmed = {k: v for k, v in result.items() if k != "chunks"}
        trimmed["chunks"] = [
            {
                "chunk_id": chunk["chunk_id"],
                "filename": chunk["filename"],
                "page_number": chunk["page_number"],
                "score": chunk["score"],
            }
            for chunk in result["chunks"]
        ]
        return trimmed
    return result
