"""PyMuPDF parsing, recursive character chunking, and LLM metadata extraction.

Chunking targets 400 tokens per chunk with a 15% (~60 token) overlap, applied
per page so every chunk keeps a truthful `page_number` for citations.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

import fitz  # PyMuPDF

from .config import settings
from .nim import (
    NIMSchemaUnsupportedError,
    NIMTimeoutError,
    chat_completion,
    embed_texts,
)
from .schemas import ExtractedDocumentMetadata

logger = logging.getLogger("lifeos.ingestion")

# --------------------------------------------------------------------------
# Token accounting
# --------------------------------------------------------------------------

# Nemotron's own tokenizer isn't published for local use, so chunk sizes are
# measured with a word/punctuation tokenizer. For English prose this tracks
# BPE token counts closely enough to honour the 400/60-token budget.
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")

# Recursive split order: paragraph, line, sentence, word, then hard split.
_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def estimate_tokens(text: str) -> int:
    """Approximate the model token count of `text`."""
    return len(_TOKEN_RE.findall(text))


# --------------------------------------------------------------------------
# Data carriers
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PageText:
    page_number: int
    text: str
    has_images: bool = False


@dataclass(slots=True)
class Chunk:
    page_number: int
    chunk_index: int
    content: str


class UnreadableDocumentError(ValueError):
    """The PDF could not be parsed at all (corrupt, encrypted, or not a PDF)."""


class ExtractionError(RuntimeError):
    """The model could not produce schema-valid metadata after a retry."""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def extract_pages(pdf_bytes: bytes) -> list[PageText]:
    """Extract per-page text with PyMuPDF, preserving page numbers for citations.

    Real-world hardening over plain ``get_text``:

    - ``sort=True`` keeps multi-column / table reading order truthful.
    - AcroForm widget values are appended explicitly — filled form fields
      often have no appearance stream, so ``get_text`` alone drops them.
    - ``has_images`` flags scanned pages (no text layer, only images) so the
      caller can explain *why* a document is unreadable instead of a generic
      "empty file" message.
    """
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed, non-500 error
        raise UnreadableDocumentError(f"Could not open PDF: {exc}") from exc

    try:
        if document.needs_pass:
            raise UnreadableDocumentError("This PDF is password-protected.")
        pages: list[PageText] = []
        for index, page in enumerate(document, start=1):
            try:
                text = page.get_text("text", sort=True) or ""
                if not text.strip():
                    # Block-level fallback recovers text from PDFs whose
                    # content stream defeats plain text mode.
                    try:
                        blocks = page.get_text("blocks", sort=True) or []
                        text = "\n".join(
                            block[4] for block in blocks if len(block) > 4 and block[4].strip()
                        )
                    except Exception:  # noqa: BLE001 - fallback is best-effort
                        logger.warning(
                            "Block fallback failed on page %s", index, exc_info=True
                        )
                text += _widget_text(page, text)
                try:
                    has_images = bool(page.get_images(full=True))
                except Exception:  # noqa: BLE001 - flag is advisory only
                    has_images = False
            except Exception:  # noqa: BLE001 - one bad page must not kill the upload
                logger.warning("Failed to extract text from page %s", index, exc_info=True)
                text, has_images = "", False
            pages.append(PageText(page_number=index, text=text, has_images=has_images))
        return pages
    finally:
        document.close()


def _widget_text(page: fitz.Page, existing: str) -> str:
    """Return AcroForm field values missing from the extracted text."""
    try:
        widgets = list(page.widgets() or [])
    except Exception:  # noqa: BLE001 - widgets are a bonus, never fatal
        return ""
    if not widgets:
        return ""
    extra: list[str] = []
    for widget in widgets:
        try:
            value = (widget.field_value or "")
            if isinstance(value, bool):
                continue
            value = str(value).strip()
        except Exception:  # noqa: BLE001 - skip unreadable widgets
            continue
        if len(value) >= 2 and value not in existing and value not in extra:
            label = (widget.field_name or widget.field_type_string or "Field").strip()
            extra.append(f"{label}: {value}")
    return ("\n" + "\n".join(extra)) if extra else ""


def total_characters(pages: Sequence[PageText]) -> int:
    return sum(len(page.text.strip()) for page in pages)


def full_text(pages: Sequence[PageText]) -> str:
    return "\n\n".join(page.text for page in pages if page.text.strip())


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def chunk_pages(
    pages: Sequence[PageText],
    *,
    chunk_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> list[Chunk]:
    """Split pages into overlapping chunks, numbering them across the document."""
    chunk_tokens = chunk_tokens or settings.CHUNK_TOKENS
    overlap_tokens = settings.chunk_overlap_tokens if overlap_tokens is None else overlap_tokens

    chunks: list[Chunk] = []
    index = 0
    for page in pages:
        text = _normalize(page.text)
        if not text:
            continue
        pieces = _apply_overlap(_split_recursive(text, chunk_tokens, _SEPARATORS), overlap_tokens)
        for piece in pieces:
            content = piece.strip()
            if not content:
                continue
            chunks.append(Chunk(page_number=page.page_number, chunk_index=index, content=content))
            index += 1
    return chunks


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _split_recursive(text: str, max_tokens: int, separators: Sequence[str]) -> list[str]:
    """Greedily pack the text into <= max_tokens pieces, recursing on overflow."""
    if not text.strip():
        return []
    if estimate_tokens(text) <= max_tokens:
        return [text]
    if not separators or separators[0] == "":
        return _hard_split(text, max_tokens)

    separator, rest = separators[0], separators[1:]
    parts = text.split(separator)

    chunks: list[str] = []
    current = ""
    for position, part in enumerate(parts):
        # Keep the separator attached so re-joining preserves the original flow.
        piece = part + separator if position < len(parts) - 1 else part
        if not piece:
            continue

        if current and estimate_tokens(current + piece) > max_tokens:
            chunks.append(current)
            current = ""

        if estimate_tokens(piece) > max_tokens:
            chunks.extend(_split_recursive(piece, max_tokens, rest))
        else:
            current += piece

    if current.strip():
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """Last resort: fixed-width character windows sized from the token density."""
    tokens = estimate_tokens(text)
    if tokens <= max_tokens or tokens == 0:
        return [text]
    chars_per_token = max(1.0, len(text) / tokens)
    window = max(1, int(max_tokens * chars_per_token))
    return [text[start : start + window] for start in range(0, len(text), window)]


def _apply_overlap(pieces: Sequence[str], overlap_tokens: int) -> list[str]:
    """Prefix each chunk with the tail of its predecessor."""
    if overlap_tokens <= 0 or len(pieces) < 2:
        return list(pieces)

    overlapped = [pieces[0]]
    for previous, current in zip(pieces, pieces[1:]):
        tail = _tail_tokens(previous, overlap_tokens)
        overlapped.append(f"{tail} {current}".strip() if tail else current)
    return overlapped


def _tail_tokens(text: str, tokens: int) -> str:
    """Return the final `tokens` tokens of `text`, preserving original spacing."""
    matches = list(_TOKEN_RE.finditer(text))
    if len(matches) <= tokens:
        return text
    return text[matches[len(matches) - tokens].start() :]


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------


async def embed_documents(texts: Sequence[str]) -> list[list[float]]:
    """Embed chunk texts for storage (passage side of the retrieval pair)."""
    return await embed_texts(texts, input_type="passage")


async def embed_query(text: str) -> list[float]:
    """Embed a search query (query side of the retrieval pair)."""
    vectors = await embed_texts([text], input_type="query")
    return vectors[0]


# --------------------------------------------------------------------------
# Metadata extraction
# --------------------------------------------------------------------------

_EXTRACTION_SYSTEM = (
    "You are a precise document information extractor for a personal document "
    "management system. Read the supplied document text and return ONLY a JSON "
    "object matching the required schema.\n"
    "Rules:\n"
    "- Use null for any field you cannot determine from the text. Never guess, "
    "infer, or invent values that are not present.\n"
    "- Dates must be ISO 8601 (YYYY-MM-DD). If a date is missing or ambiguous, use null.\n"
    "- financial_amount must be a plain number: no currency symbol, no thousands "
    "separators.\n"
    "- Set task_warranted to true ONLY when the document states a concrete deadline "
    "or due date that requires the user to act (pay, renew, submit, cancel, return). "
    "A document with no stated deadline must have task_warranted = false.\n"
    "- action_description must be a short imperative phrase, e.g. 'Pay electricity bill'.\n"
    "- category must be exactly one of: Insurance, Tax, Vehicle, Utility, Warranty, "
    "Rental, General.\n"
    "Example input:\n"
    "\"\"\"Utility Bill\nIssuer: ConEd Energy\nAccount: ACC-98765\nDue Date: 2026-09-24\nAmount: $145.50\nPay electricity bill\"\"\"\n"
    "Example output:\n"
    "{\"category\":\"Utility\",\"issuer\":\"ConEd Energy\",\"identifier\":\"ACC-98765\","
    "\"financial_amount\":145.5,\"currency\":\"USD\",\"created_date\":null,"
    "\"action_deadline\":\"2026-09-24\",\"action_description\":\"Pay electricity bill\","
    "\"task_warranted\":true}"
)

_STRICT_RETRY_SUFFIX = (
    "\n\nCRITICAL: your previous reply was not valid JSON. Respond with a single "
    "raw JSON object and nothing else. No markdown code fences, no commentary, no "
    "text before or after the object. Every key must be present; use null where the "
    "value is unknown.\n"
    "Required keys: category, issuer, identifier, financial_amount, currency, "
    "created_date, action_deadline, action_description, task_warranted."
)

# Once we learn the deployment rejects json_schema, stop sending it.
_schema_supported = True


def _structured_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "ExtractedDocumentMetadata",
            "schema": ExtractedDocumentMetadata.model_json_schema(),
            "strict": True,
        },
    }


def _build_extraction_messages(excerpt: str, *, strict: bool) -> list[dict[str, str]]:
    system = _EXTRACTION_SYSTEM + (_STRICT_RETRY_SUFFIX if strict else "")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Document text:\n\n{excerpt}"},
    ]


async def extract_metadata(raw_text: str) -> ExtractedDocumentMetadata:
    """Extract structured metadata, retrying once with a stricter prompt.

    Raises ExtractionError if both attempts fail, which the caller turns into
    `documents.status = 'needs_review'` rather than a 500.

    After LLM extraction, a lightweight regex fallback enriches obvious misses
    (deadline, amount, issuer) — the model often returns partial JSON, so a
    2026-10-10 in the text should not be lost just because the LLM omitted it.
    """
    global _schema_supported

    excerpt = raw_text[: settings.EXTRACTION_CHAR_LIMIT]
    last_error: Exception | None = None

    for attempt in (1, 2):
        strict = attempt == 2
        messages = _build_extraction_messages(excerpt, strict=strict)
        try:
            if not strict and _schema_supported:
                try:
                    response = await chat_completion(
                        messages,
                        response_format=_structured_response_format(),
                        temperature=0.0,
                        timeout=settings.EXTRACTION_TIMEOUT_SECONDS,
                        retries=settings.EXTRACTION_TIMEOUT_RETRIES,
                    )
                except NIMSchemaUnsupportedError:
                    logger.warning("NIM rejected json_schema; falling back to prompt-only JSON.")
                    _schema_supported = False
                    response = await chat_completion(
                        messages,
                        temperature=0.0,
                        timeout=settings.EXTRACTION_TIMEOUT_SECONDS,
                    )
            else:
                response = await chat_completion(
                    messages,
                    temperature=0.0,
                    timeout=settings.EXTRACTION_TIMEOUT_SECONDS,
                )

            payload = _message_text(response)
            validated = ExtractedDocumentMetadata.model_validate_json(_strip_code_fence(payload))
            enriched = _enrich_from_text(validated, raw_text)
            # If first attempt was too sparse but fallback filled a deadline, return enriched
            # immediately — no need to retry. If still very sparse, retry once for completeness.
            if attempt == 1 and _is_sparse(validated) and not _is_sparse(enriched):
                return enriched
            if attempt == 1 and _is_sparse(enriched):
                last_error = ValueError(f"Extraction too sparse: {payload[:500]}")
                logger.warning("Extraction sparse on attempt 1, retrying strict: %s", payload[:300])
                continue
            return enriched
        except NIMTimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - retried once, then surfaced
            last_error = exc
            logger.warning("Extraction attempt %s failed: %s", attempt, exc)

    raise ExtractionError(str(last_error) if last_error else "Extraction failed.")


def _is_sparse(md: ExtractedDocumentMetadata) -> bool:
    """True when the model returned almost nothing — trigger a strict retry."""
    filled = sum(
        1
        for v in [md.issuer, md.identifier, md.financial_amount, md.action_deadline, md.created_date]
        if v is not None
    )
    return filled == 0 and not md.action_description


def _enrich_from_text(md: ExtractedDocumentMetadata, raw_text: str) -> ExtractedDocumentMetadata:
    """Fill obvious gaps from raw text when the LLM omitted them."""
    # Deadlines are critical for auto-tasks — regex is reliable for YYYY-MM-DD.
    if md.action_deadline is None and md.task_warranted:
        iso = _regex_iso_date(raw_text)
        if iso:
            try:
                md = md.model_copy(update={"action_deadline": iso})
            except Exception:
                pass
    # If task_warranted is true but action_description missing, use a generic one.
    if md.task_warranted and not md.action_description:
        try:
            md = md.model_copy(update={"action_description": f"{md.category} action required"})
        except Exception:
            pass
    # Financial amount fallback: $1,234.56 or 145.50
    if md.financial_amount is None:
        amt = _regex_amount(raw_text)
        if amt is not None:
            try:
                md = md.model_copy(update={"financial_amount": amt})
            except Exception:
                pass
    # Issuer fallback: "Issuer: XYZ" or "Provider: XYZ"
    if md.issuer is None:
        issuer = _regex_issuer(raw_text)
        if issuer:
            try:
                md = md.model_copy(update={"issuer": issuer})
            except Exception:
                pass
    return md


def _regex_iso_date(text: str) -> str | None:
    from app.schemas import coerce_iso_date

    # Prefer explicit YYYY-MM-DD already in text.
    for m in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b", text):
        iso = coerce_iso_date(m.group(0))
        if iso:
            return iso
    # Fallback: other date forms that coerce_iso_date can handle.
    for m in re.finditer(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b", text):
        iso = coerce_iso_date(m.group(0))
        if iso:
            return iso
    return None


def _regex_amount(text: str) -> float | None:
    m = re.search(r"\$\s*([0-9,]+\.\d{2})", text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"\b([0-9]+\.\d{2})\s*(?:USD|INR|EUR)?\b", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _regex_issuer(text: str) -> str | None:
    for pattern in [r"Issuer:\s*([^\n]+)", r"Provider:\s*([^\n]+)", r"Utility:\s*([^\n]+)", r"From:\s*([^\n]+)"]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().split("\n")[0].strip()
            if len(val) >= 3 and len(val) <= 60:
                return val
    return None


def _message_text(response: Any) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError) as exc:
        raise ExtractionError("Model returned an empty response.") from exc


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_code_fence(text: str) -> str:
    """Models sometimes wrap JSON in markdown fences despite instructions."""
    stripped = _FENCE_RE.sub("", text.strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped
