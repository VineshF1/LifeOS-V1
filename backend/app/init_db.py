"""Apply schema.sql to the configured database.

Usage:
    python -m app.init_db

Idempotent: every statement uses IF NOT EXISTS or DROP-then-CREATE, so it is
safe to re-run against an existing Neon branch.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg

from .config import settings
from .database import normalize_database_url

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


async def main() -> int:
    if not SCHEMA_PATH.exists():
        print(f"schema.sql not found at {SCHEMA_PATH}", file=sys.stderr)
        return 1

    dsn, connect_args = normalize_database_url(settings.DATABASE_URL)
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)

    print(f"Applying schema to {dsn.split('@')[-1]} ...")
    connection = await asyncpg.connect(dsn, **connect_args)
    try:
        # No parameters => simple query protocol => multi-statement script is fine.
        await connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        version = await connection.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        tables = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
    finally:
        await connection.close()

    print(f"pgvector {version}")
    print("Tables: " + ", ".join(row["tablename"] for row in tables))
    print("Schema applied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
