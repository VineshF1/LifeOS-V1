"""Pydantic v2 request/response schemas."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

_NULLISH = {"", "null", "none", "n/a", "na", "unknown", "-", "not specified"}
_AMOUNT_NOISE_RE = re.compile(r"[^0-9.\-]")
_DATE_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"),
)


def coerce_iso_date(value: Any) -> str | None:
    """Coerce a model-supplied date into YYYY-MM-DD, or None when unusable.

    Dates arrive from an LLM, so 'Due 15/03/2026' and 'March 15, 2026' both need
    to either become a real ISO date or become None -- never a bogus DATE value.
    """
    if value is None:
        return None
    if isinstance(value, datetime):  # must precede the `date` check (subclass)
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if text.lower() in _NULLISH:
        return None

    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        first, second, third = match.groups()
        if len(first) == 4:  # YYYY-MM-DD
            try:
                return date(int(first), int(second), int(third)).isoformat()
            except ValueError:
                continue
        # Ambiguous numeric date. Try day-first (15/03/2026) then month-first
        # (03/15/2026) -- only one ordering can be a real calendar date whenever
        # either field exceeds 12.
        for day, month in ((first, second), (second, first)):
            try:
                return date(int(third), int(month), int(day)).isoformat()
            except ValueError:
                continue
    return None


def _strip_to_float(raw: str) -> float | None:
    text = raw.strip()
    if text.lower() in _NULLISH:
        return None
    if "," in text and "." in text:
        # 1.234,56 -> 1234.56 ; 1,234.56 -> 1234.56
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        # A single comma is a decimal separator only when 1-2 digits follow.
        text = text.replace(",", "." if len(text.rsplit(",", 1)[-1]) in (1, 2) else "")
    cleaned = _AMOUNT_NOISE_RE.sub("", text)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


class ExtractedDocumentMetadata(BaseModel):
    category: Literal["Insurance", "Tax", "Vehicle", "Utility", "Warranty", "Rental", "General"]
    issuer: Optional[str] = Field(None, description="Provider, utility, insurer, or landlord name")
    identifier: Optional[str] = Field(None, description="Policy number, account number, or invoice ID")
    financial_amount: Optional[float] = Field(None, description="Total amount due, premium, or deposit")
    currency: Optional[str] = "USD"
    created_date: Optional[str] = Field(None, description="Issue date in YYYY-MM-DD")
    action_deadline: Optional[str] = Field(None, description="Due date, expiration, or renewal date in YYYY-MM-DD")
    action_description: Optional[str] = Field(None, description="Specific action, e.g., 'Pay electricity bill'")
    task_warranted: bool = Field(False, description="True if a concrete actionable task must be scheduled")

    # The model is instructed to emit clean values, but it also has to tolerate
    # realistic ones ('$1,234.56', 'Due 15/03/2026') without losing a whole
    # document to a validation error.
    @field_validator("created_date", "action_deadline", mode="before")
    @classmethod
    def _normalize_dates(cls, value: Any) -> Any:
        return coerce_iso_date(value)

    @field_validator("financial_amount", mode="before")
    @classmethod
    def _normalize_amount(cls, value: Any) -> Any:
        if value is None or isinstance(value, (int, float)):
            return value
        return _strip_to_float(str(value))

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: Any) -> Any:
        if value is None:
            return "USD"
        letters = re.sub(r"[^A-Za-z]", "", str(value)).upper()
        return letters if len(letters) == 3 else "USD"


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


class DocumentOut(BaseModel):
    id: UUID
    filename: str
    category: str
    status: str
    has_actionable_deadline: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    chunk_count: int = 0


class DocumentListResponse(BaseModel):
    documents: list[DocumentOut]


class UploadResponse(BaseModel):
    document: DocumentOut
    message: str | None = None
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    document_id: UUID | None = Field(
        None, description="Restrict retrieval to one document; omit to search every document."
    )


class Citation(BaseModel):
    chunk_id: UUID
    filename: str
    page_number: int
    excerpt: str


class ToolTrace(BaseModel):
    iteration: int
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = ""
    execution_time_ms: int = 0


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    tool_trace: list[ToolTrace] = Field(default_factory=list)
    iterations: int = 0


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


class TaskOut(BaseModel):
    id: UUID
    document_id: UUID | None = None
    title: str
    due_date: date | None = None
    status: str
    created_at: datetime
    source_filename: str | None = None


class TaskStatusUpdate(BaseModel):
    status: Literal["pending", "completed"]


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


class AuditLogOut(BaseModel):
    id: UUID
    action_type: str
    tool_name: str | None = None
    input_payload: dict[str, Any] | None = None
    output_payload: dict[str, Any] | None = None
    execution_time_ms: int | None = None
    created_at: datetime


class ErrorResponse(BaseModel):
    status: str = "error"
    message: str
