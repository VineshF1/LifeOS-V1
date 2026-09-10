"""PDF upload, parsing, chunked embedding, metadata extraction, and listing."""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent.tools import ToolContext, create_task
from ..config import settings
from ..database import get_current_user_id, get_tenant_session, record_audit, to_vector_literal
from ..ingestion import (
    ExtractionError,
    UnreadableDocumentError,
    chunk_pages,
    embed_documents,
    extract_metadata,
    extract_pages,
    full_text,
    total_characters,
)
from ..nim import NIMTimeoutError, NIMUnavailableError
from ..schemas import DocumentListResponse, DocumentOut, UploadResponse

logger = logging.getLogger("lifeos.documents")

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PDF_MAGIC = b"%PDF-"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    """Metadata arrives as jsonb; tolerate drivers that hand back raw text."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _document_out(row: Any) -> DocumentOut:
    return DocumentOut(
        id=row["id"],
        filename=row["filename"],
        category=row["category"] or "General",
        status=row["status"] or "processing",
        has_actionable_deadline=bool(row["has_actionable_deadline"]),
        metadata=_as_dict(row["metadata"]),
        created_at=row["created_at"],
        chunk_count=int(row["chunk_count"]) if "chunk_count" in row.keys() else 0,
    )


def _safe_filename(raw: str | None) -> str:
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable())
    return (name or "document.pdf")[:255]


async def _set_status(session: AsyncSession, document_id: UUID, status_value: str) -> None:
    await session.execute(
        text(
            """UPDATE documents SET status = :status
               WHERE id = CAST(CAST(:document_id AS text) AS uuid)"""
        ),
        {"status": status_value, "document_id": str(document_id)},
    )


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> UploadResponse:
    """Ingest one PDF.

    Returns 200/201 with a document whose `status` may be `needs_review` rather
    than failing the request: a bad file must never break the dashboard.

    Order of operations is deliberate -- chunks are embedded BEFORE extraction,
    so a document whose extraction fails is still fully searchable in chat.
    """
    filename = _safe_filename(file.filename)
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are supported."
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
        )
    if PDF_MAGIC not in data[:1024]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="That file is not a valid PDF."
        )

    started = time.perf_counter()
    document_id = uuid4()
    warnings: list[str] = []

    # Row first, so any later failure still leaves a visible, explainable document.
    await session.execute(
        text(
            """
            INSERT INTO documents (id, user_id, filename, status, raw_text, metadata)
            VALUES (
                CAST(CAST(:id AS text) AS uuid),
                CAST(CAST(:user_id AS text) AS uuid),
                :filename, 'processing', '', CAST(CAST(:metadata AS text) AS jsonb)
            )
            """
        ),
        {"id": str(document_id), "user_id": str(user_id), "filename": filename, "metadata": "{}"},
    )

    # --- 1. Parse ---------------------------------------------------------
    try:
        pages = extract_pages(data)
    except UnreadableDocumentError as exc:
        await _set_status(session, document_id, "needs_review")
        warnings.append(f"{exc} The file was stored but could not be read.")
        await record_audit(
            session,
            user_id,
            "document_upload",
            output_payload={"filename": filename, "error": str(exc)},
            execution_time_ms=int((time.perf_counter() - started) * 1000),
        )
        return UploadResponse(
            document=await _load_document(session, document_id),
            message="Document stored, but its text could not be read.",
            warnings=warnings,
        )

    raw_text = full_text(pages)
    character_count = total_characters(pages)

    if character_count < settings.MIN_EXTRACTABLE_CHARS:
        scanned = any(page.has_images for page in pages)
        if scanned:
            message = (
                "This PDF looks like scanned images with no text layer, so no text "
                "could be extracted. Please upload a searchable PDF (or export/print "
                "the scan to PDF with OCR first)."
            )
            error_code = "scanned_no_text"
        else:
            message = "Document text is unreadable or empty. Please check the file."
            error_code = "insufficient_text"
        await session.execute(
            text(
                """UPDATE documents
                   SET status = 'needs_review', raw_text = :raw_text,
                       metadata = CAST(CAST(:metadata AS text) AS jsonb)
                   WHERE id = CAST(CAST(:document_id AS text) AS uuid)"""
            ),
            {
                "raw_text": raw_text,
                "metadata": json.dumps({"extraction_error": error_code}),
                "document_id": str(document_id),
            },
        )
        warnings.append(message)
        await record_audit(
            session,
            user_id,
            "document_upload",
            output_payload={
                "filename": filename,
                "characters": character_count,
                "scanned": scanned,
            },
            execution_time_ms=int((time.perf_counter() - started) * 1000),
        )
        return UploadResponse(
            document=await _load_document(session, document_id),
            message=message,
            warnings=warnings,
        )

    # --- 2. Chunk + embed (before extraction, so search works regardless) ---
    chunks = chunk_pages(pages)
    chunk_count = 0
    if chunks:
        try:
            vectors = await embed_documents([chunk.content for chunk in chunks])
            await _insert_chunks(session, document_id, user_id, filename, chunks, vectors)
            chunk_count = len(chunks)
        except Exception as exc:  # noqa: BLE001 - upload must survive an embedding outage
            logger.warning("Embedding failed for %s: %s", filename, exc)
            warnings.append(
                "Vector indexing failed, so this document is not yet searchable in chat."
            )

    # --- 3. Structured extraction ----------------------------------------
    status_value = "ready"
    metadata_payload: dict[str, Any] = {}
    has_deadline = False
    extraction_ms = 0

    extraction_started = time.perf_counter()
    try:
        extracted = await extract_metadata(raw_text)
        metadata_payload = extracted.model_dump()
        has_deadline = bool(extracted.task_warranted and extracted.action_deadline)
        if has_deadline:
            task_result = await create_task(
                ToolContext(session=session, user_id=user_id, document_id=document_id),
                title=extracted.action_description or f"{extracted.category} action required",
                due_date=extracted.action_deadline,
                source_document_id=str(document_id),
            )
            if "error" in task_result:
                warnings.append(f"Could not draft the task automatically: {task_result['error']}")
                has_deadline = False
            else:
                metadata_payload["drafted_task_id"] = task_result["task_id"]
    except ExtractionError as exc:
        # Both attempts failed: keep the document, flag it for review, and leave
        # it indexed for vector Q&A.
        status_value = "needs_review"
        metadata_payload = {"extraction_error": str(exc)[:500]}
        has_deadline = False
        warnings.append(
            "Automatic field extraction failed. The document is still searchable in chat."
        )
    except (NIMTimeoutError, NIMUnavailableError) as exc:
        # NIM outage/timeout during extraction must not become a 504 for upload.
        # Keep the document searchable and flag for review; user can retry extraction
        # later. This is the correct fallback per Prompt §4 (never 500 the dashboard).
        logger.warning("Extraction NIM error for %s: %s", filename, exc)
        status_value = "needs_review"
        metadata_payload = {"extraction_error": str(exc)[:500], "extraction_timeout": True}
        has_deadline = False
        warnings.append(
            "AI extraction timed out — document is stored and searchable, but fields need review. Please try re-uploading in a moment."
        )
    except Exception as exc:  # noqa: BLE001 - upload must survive any extraction crash
        logger.warning("Unexpected extraction failure for %s: %s", filename, exc, exc_info=True)
        status_value = "needs_review"
        metadata_payload = {"extraction_error": str(exc)[:500]}
        has_deadline = False
        warnings.append(
            "Automatic field extraction failed. The document is still searchable in chat."
        )
    finally:
        extraction_ms = int((time.perf_counter() - extraction_started) * 1000)

    await session.execute(
        text(
            """
            UPDATE documents
            SET status = :status,
                raw_text = :raw_text,
                category = :category,
                metadata = CAST(CAST(:metadata AS text) AS jsonb),
                has_actionable_deadline = :has_deadline
            WHERE id = CAST(CAST(:document_id AS text) AS uuid)
            """
        ),
        {
            "status": status_value,
            "raw_text": raw_text,
            "category": metadata_payload.get("category") or "General",
            "metadata": json.dumps(metadata_payload, default=str),
            "has_deadline": has_deadline,
            "document_id": str(document_id),
        },
    )

    total_ms = int((time.perf_counter() - started) * 1000)
    await record_audit(
        session,
        user_id,
        "document_upload",
        output_payload={
            "filename": filename,
            "characters": character_count,
            "chunks": chunk_count,
            "status": status_value,
        },
        execution_time_ms=total_ms,
    )
    await record_audit(
        session,
        user_id,
        "extraction",
        output_payload=metadata_payload,
        execution_time_ms=extraction_ms,
    )

    return UploadResponse(
        document=await _load_document(session, document_id),
        message=None,
        warnings=warnings,
    )


async def _insert_chunks(
    session: AsyncSession,
    document_id: UUID,
    user_id: UUID,
    filename: str,
    chunks: list[Any],
    vectors: list[list[float]],
) -> None:
    statement = text(
        """
        INSERT INTO document_chunks
            (document_id, user_id, filename, page_number, chunk_index, content, embedding)
        VALUES (
            CAST(CAST(:document_id AS text) AS uuid),
            CAST(CAST(:user_id AS text) AS uuid),
            :filename, :page_number, :chunk_index, :content,
            CAST(CAST(:embedding AS text) AS vector(2048))
        )
        """
    )
    params = [
        {
            "document_id": str(document_id),
            "user_id": str(user_id),
            "filename": filename,
            "page_number": chunk.page_number,
            "chunk_index": chunk.chunk_index,
            "content": chunk.content,
            "embedding": to_vector_literal(vector),
        }
        for chunk, vector in zip(chunks, vectors)
    ]
    # One round trip per batch rather than per chunk.
    for start in range(0, len(params), 50):
        await session.execute(statement, params[start : start + 50])


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

_SELECT_DOCUMENTS = """
    SELECT d.id, d.filename, d.category, d.status, d.has_actionable_deadline,
           d.metadata, d.created_at, COUNT(c.id) AS chunk_count
    FROM documents d
    LEFT JOIN document_chunks c
           ON c.document_id = d.id AND c.user_id = d.user_id
    WHERE {where}
    GROUP BY d.id
    ORDER BY d.created_at DESC
"""


async def _load_document(session: AsyncSession, document_id: UUID) -> DocumentOut:
    row = (
        await session.execute(
            text(_SELECT_DOCUMENTS.format(where="d.id = CAST(CAST(:document_id AS text) AS uuid)")),
            {"document_id": str(document_id)},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return _document_out(row)


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    category: str | None = Query(None, description="Filter by document category."),
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> DocumentListResponse:
    params: dict[str, Any] = {"user_id": str(user_id)}
    where = "d.user_id = CAST(CAST(:user_id AS text) AS uuid)"

    if category and category.strip():
        where += " AND d.category = :category"
        params["category"] = category.strip().title()

    rows = (await session.execute(text(_SELECT_DOCUMENTS.format(where=where)), params)).mappings().all()
    return DocumentListResponse(documents=[_document_out(row) for row in rows])


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> DocumentOut:
    return await _load_document(session, document_id)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> Response:
    result = await session.execute(
        text(
            """DELETE FROM tasks
               WHERE document_id = CAST(CAST(:document_id AS text) AS uuid)
                 AND user_id = CAST(CAST(:user_id AS text) AS uuid)"""
        ),
        {"document_id": str(document_id), "user_id": str(user_id)},
    )
    result = await session.execute(
        text("DELETE FROM documents WHERE id = CAST(CAST(:document_id AS text) AS uuid)"),
        {"document_id": str(document_id)},
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
