"""Agentic chat endpoint with programmatic, hallucination-proof citations.

The model never writes source blocks. It emits `[REF: <chunk_uuid>]`, and this
module resolves each tag against the chunks that were actually shown to the
model, then injects the verbatim stored text. A ref to a chunk the model was
never given is deleted rather than rendered.
"""
from __future__ import annotations

import logging
import re
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent.loop import run_agent
from ..database import get_current_user_id, get_tenant_session, record_audit
from ..schemas import ChatRequest, ChatResponse, Citation

logger = logging.getLogger("lifeos.chat")

router = APIRouter(prefix="/chat", tags=["chat"])

_REF_RE = re.compile(
    r"\[REF:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*\]"
)
_EXCERPT_WORDS = 20


def _excerpt(content: str) -> str:
    """First 20 words of the stored chunk, verbatim."""
    words = content.split()
    if len(words) <= _EXCERPT_WORDS:
        return content.strip()
    return " ".join(words[:_EXCERPT_WORDS]) + "..."


def render_citations(
    answer: str, seen_chunks: dict[str, dict]
) -> tuple[str, list[Citation]]:
    """Replace every [REF: ...] tag with a resolved, verbatim [Source: ...] tag."""
    ordered: dict[str, Citation] = {}

    def _replace(match: re.Match[str]) -> str:
        chunk_id = match.group(1).lower()
        chunk = seen_chunks.get(chunk_id)
        if chunk is None:
            # Cited a chunk that was never retrieved -- drop the tag rather than
            # display a source that does not exist.
            logger.warning("Dropped citation to unretrieved chunk %s", chunk_id)
            return ""

        excerpt = _excerpt(chunk["content"])
        ordered.setdefault(
            chunk_id,
            Citation(
                chunk_id=UUID(chunk_id),
                filename=chunk["filename"],
                page_number=int(chunk["page_number"]),
                excerpt=excerpt,
            ),
        )
        inline_excerpt = excerpt.replace('"', "'")
        return (
            f'[Source: {chunk["filename"]}, Page: {chunk["page_number"]}, '
            f'Excerpt: "{inline_excerpt}"]'
        )

    rendered = _REF_RE.sub(_replace, answer)
    rendered = re.sub(r"[ \t]{2,}", " ", rendered)
    return rendered.strip(), list(ordered.values())


@router.post("", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> ChatResponse:
    # RLS already scopes this lookup; an unknown id simply returns no row.
    if payload.document_id is not None:
        exists = await session.scalar(
            text(
                """SELECT 1 FROM documents
                   WHERE id = CAST(CAST(:document_id AS text) AS uuid)"""
            ),
            {"document_id": str(payload.document_id)},
        )
        if not exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Document not found."
            )

    started = time.perf_counter()
    result = await run_agent(session, user_id, payload.message, payload.document_id)
    answer, citations = render_citations(result.answer, result.seen_chunks)
    execution_ms = int((time.perf_counter() - started) * 1000)

    await record_audit(
        session,
        user_id,
        "chat_turn",
        input_payload={
            "message": payload.message,
            "document_id": str(payload.document_id) if payload.document_id else None,
        },
        output_payload={
            "answer": answer,
            "iterations": result.iterations,
            "tools": [trace.model_dump(mode="json") for trace in result.tool_trace],
            "citations": [citation.model_dump(mode="json") for citation in citations],
        },
        execution_time_ms=execution_ms,
    )

    return ChatResponse(
        answer=answer,
        citations=citations,
        tool_trace=result.tool_trace,
        iterations=result.iterations,
    )
