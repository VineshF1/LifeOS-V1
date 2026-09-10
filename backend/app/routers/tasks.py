"""Actionable task listing, status toggling, and deletion."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_current_user_id, get_tenant_session, record_audit
from ..schemas import TaskOut, TaskStatusUpdate

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Soonest deadline first; tasks with no deadline sort last.
_SELECT_TASKS = """
    SELECT t.id, t.document_id, t.title, t.due_date, t.status, t.created_at,
           d.filename AS source_filename
    FROM tasks t
    LEFT JOIN documents d ON d.id = t.document_id AND d.user_id = t.user_id
    WHERE {where}
    ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.created_at DESC
"""


def _task_out(row: Any) -> TaskOut:
    return TaskOut(
        id=row["id"],
        document_id=row["document_id"],
        title=row["title"],
        due_date=row["due_date"],
        status=row["status"],
        created_at=row["created_at"],
        source_filename=row["source_filename"],
    )


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    task_status: str | None = Query(
        None, alias="status", description="Filter by 'pending' or 'completed'."
    ),
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> list[TaskOut]:
    params: dict[str, Any] = {"user_id": str(user_id)}
    where = "t.user_id = CAST(CAST(:user_id AS text) AS uuid)"

    if task_status and task_status.strip():
        normalized = task_status.strip().lower()
        if normalized not in {"pending", "completed"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="status must be 'pending' or 'completed'.",
            )
        where += " AND t.status = :status"
        params["status"] = normalized

    rows = (await session.execute(text(_SELECT_TASKS.format(where=where)), params)).mappings().all()
    return [_task_out(row) for row in rows]


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task_status(
    task_id: UUID,
    payload: TaskStatusUpdate,
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> TaskOut:
    result = await session.execute(
        text(
            """UPDATE tasks SET status = :status
               WHERE id = CAST(CAST(:task_id AS text) AS uuid)"""
        ),
        {"status": payload.status, "task_id": str(task_id)},
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")

    rows = (
        await session.execute(
            text(_SELECT_TASKS.format(where="t.id = CAST(CAST(:task_id AS text) AS uuid)")),
            {"task_id": str(task_id)},
        )
    ).mappings().all()

    await record_audit(
        session,
        user_id,
        "task_status_update",
        input_payload={"task_id": str(task_id), "status": payload.status},
        output_payload={"updated": result.rowcount},
    )

    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return _task_out(rows[0])


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_tenant_session),
) -> Response:
    """Delete a task — including orphans whose document is already gone."""
    result = await session.execute(
        text(
            """DELETE FROM tasks
               WHERE id = CAST(CAST(:task_id AS text) AS uuid)
                 AND user_id = CAST(CAST(:user_id AS text) AS uuid)"""
        ),
        {"task_id": str(task_id), "user_id": str(user_id)},
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
