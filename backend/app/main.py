"""FastAPI application: CORS, routers, and centralised exception handling.

External inference failures are translated here into clean JSON responses, so
no upstream timeout can surface as an HTML 500 or leave a request hanging.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .config import settings
from .database import engine
from .nim import NIMTimeoutError, NIMUnavailableError, close_client
from .routers import auth, chat, documents, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("lifeos")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _verify_database()
    yield
    await close_client()
    await engine.dispose()


async def _verify_database() -> None:
    """Log database health at startup without preventing the app from serving."""
    try:
        async with engine.connect() as connection:
            vector_version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            has_tables = await connection.scalar(text("SELECT to_regclass('public.documents')"))
        if vector_version is None:
            logger.error("pgvector is not installed. Run: python -m app.init_db")
        elif has_tables is None:
            logger.error("Schema missing. Run: python -m app.init_db")
        else:
            logger.info("Database ready (pgvector %s).", vector_version)
    except Exception as exc:  # noqa: BLE001 - the API must still boot
        logger.error("Database unreachable at startup (%s). Run: python -m app.init_db", exc)


app = FastAPI(
    title="LifeOS Agent API",
    version="1.0.0",
    description="Privacy-first personal document and life management agent.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(NIMTimeoutError)
async def _handle_nim_timeout(request: Request, exc: NIMTimeoutError) -> JSONResponse:
    logger.warning("NIM timeout on %s", request.url.path)
    return JSONResponse(status_code=504, content={"status": "error", "message": str(exc)})


@app.exception_handler(NIMUnavailableError)
async def _handle_nim_unavailable(request: Request, exc: NIMUnavailableError) -> JSONResponse:
    logger.error("NIM unavailable on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=502,
        content={"status": "error", "message": f"AI service unavailable: {exc}"},
    )


@app.exception_handler(Exception)
async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "An unexpected server error occurred."},
    )


app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(tasks.router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {
        "status": "ok",
        "llm_model": settings.LLM_MODEL,
        "embedding_model": settings.EMBEDDING_MODEL,
        "embedding_dim": settings.EMBEDDING_DIM,
        "agent_max_iterations": settings.AGENT_MAX_ITERATIONS,
        "agents_configured": bool(settings.NVIDIA_API_KEY),
    }


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {
        "service": "LifeOS Agent API",
        "docs": "/docs",
        "endpoints": [
            "POST /auth/signup",
            "POST /auth/login",
            "GET  /auth/me",
            "POST /documents/upload",
            "GET  /documents",
            "GET  /documents/{id}",
            "DELETE /documents/{id}",
            "POST /chat",
            "GET  /tasks",
            "PATCH /tasks/{id}",
        ],
    }
