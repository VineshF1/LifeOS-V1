"""Shared NVIDIA NIM client: embeddings and chat completions with timeout guards.

Every outbound call is bounded by settings.NIM_TIMEOUT_SECONDS so a slow or
rate-limited upstream can never suspend a FastAPI worker indefinitely.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

import httpx
from openai import APITimeoutError, AsyncOpenAI, BadRequestError

from .config import settings

logger = logging.getLogger("lifeos.nim")


_TIMEOUT_MESSAGE = "AI inference timed out. Please retry in a moment."


class NIMTimeoutError(RuntimeError):
    """Raised when an upstream NIM call exceeds the client timeout."""


class NIMUnavailableError(RuntimeError):
    """Raised when NIM rejects or fails a call for a non-timeout reason."""


class NIMSchemaUnsupportedError(NIMUnavailableError):
    """The deployment rejected a request-shape parameter (e.g. response_format).

    Callers treat this as "retry without the optional parameter" rather than a
    hard failure.
    """


client = AsyncOpenAI(
    base_url=settings.NVIDIA_BASE_URL,
    api_key=settings.NVIDIA_API_KEY or "missing-nvidia-api-key",
    timeout=settings.NIM_TIMEOUT_SECONDS,
    max_retries=1,
    http_client=httpx.AsyncClient(timeout=settings.NIM_TIMEOUT_SECONDS),
)


async def embed_texts(texts: Sequence[str], *, input_type: str = "passage") -> list[list[float]]:
    """Embed texts with nemotron-3-embed-1b (native 2048 dims).

    `dimensions` is deliberately never sent: the model only accepts its native
    2048 and returns HTTP 400 for anything else.
    """
    if not texts:
        return []

    vectors: list[list[float]] = []
    for start in range(0, len(texts), settings.EMBEDDING_BATCH_SIZE):
        batch = list(texts[start : start + settings.EMBEDDING_BATCH_SIZE])
        response = await _embed_batch(batch, input_type)
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors.extend([list(item.embedding) for item in ordered])

    dims = {len(v) for v in vectors}
    if dims != {settings.EMBEDDING_DIM}:
        raise NIMUnavailableError(
            f"Embedding dimension mismatch: expected {settings.EMBEDDING_DIM}, got {sorted(dims)}."
        )
    # A short response would be silently swallowed by the caller's zip(), storing
    # fewer chunks than it reports. Fail loudly instead.
    if len(vectors) != len(texts):
        raise NIMUnavailableError(
            f"Embedding count mismatch: sent {len(texts)} texts, got {len(vectors)} vectors."
        )
    return vectors


async def _embed_batch(batch: list[str], input_type: str) -> Any:
    extra_body = {"input_type": input_type} if input_type else None
    try:
        return await client.embeddings.create(
            model=settings.EMBEDDING_MODEL, input=batch, extra_body=extra_body
        )
    except BadRequestError:
        # Some NIM deployments don't expose `input_type`; retry without it
        # rather than failing the whole ingestion.
        if not extra_body:
            raise
        logger.warning("Embedding endpoint rejected input_type; retrying without it.")
        try:
            return await client.embeddings.create(model=settings.EMBEDDING_MODEL, input=batch)
        except (httpx.TimeoutException, APITimeoutError) as exc:
            raise NIMTimeoutError(_TIMEOUT_MESSAGE) from exc
        except Exception as exc:  # noqa: BLE001 - normalised for the API layer
            raise NIMUnavailableError(f"Embedding request failed: {exc}") from exc
    except (httpx.TimeoutException, APITimeoutError) as exc:
        raise NIMTimeoutError(_TIMEOUT_MESSAGE) from exc
    except Exception as exc:  # noqa: BLE001
        raise NIMUnavailableError(f"Embedding request failed: {exc}") from exc


async def chat_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    timeout: float | None = None,
    retries: int = 0,
) -> Any:
    """Single chat-completions turn against the Nemotron-3 agent model.

    `timeout` overrides the default 15s guardrail for callers whose prompts
    are large (agent turns); `retries` repeats once on timeout because NIM
    stalls are usually transient. Extraction keeps the strict default since
    its caller already degrades gracefully.
    """
    kwargs: dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or settings.AGENT_MAX_TOKENS,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    if response_format:
        kwargs["response_format"] = response_format
    if timeout:
        kwargs["timeout"] = timeout

    attempts = 1 + max(0, retries)
    for attempt in range(1, attempts + 1):
        try:
            return await client.chat.completions.create(**kwargs)
        except (httpx.TimeoutException, APITimeoutError) as exc:
            if attempt < attempts:
                logger.warning(
                    "Chat completion timed out (attempt %s/%s); retrying once.",
                    attempt,
                    attempts,
                )
                continue
            raise NIMTimeoutError(_TIMEOUT_MESSAGE) from exc
        except BadRequestError as exc:
            raise NIMSchemaUnsupportedError(f"Request rejected by NIM: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise NIMUnavailableError(f"Chat completion failed: {exc}") from exc
    raise NIMTimeoutError(_TIMEOUT_MESSAGE)  # unreachable; keeps types honest


async def close_client() -> None:
    await client.close()
