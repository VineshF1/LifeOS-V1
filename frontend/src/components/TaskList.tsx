"use client";

import { IconInbox, IconTrash } from "@tabler/icons-react";
import type { TaskOut } from "@/lib/api";
import { daysUntil, formatDate } from "@/lib/utils";

interface TaskListProps {
  tasks: TaskOut[];
  loading: boolean;
  onToggle: (task: TaskOut) => void;
  onDelete: (task: TaskOut) => void;
  pendingId: string | null;
}

function dueBadge(task: TaskOut): { cls: string; label: string } {
  if (task.status === "completed") return { cls: "bg-green-lt", label: formatDate(task.due_date) };
  const days = daysUntil(task.due_date);
  if (days === null) return { cls: "bg-muted-lt", label: "No deadline" };
  if (days < 0) return { cls: "bg-red-lt", label: `${Math.abs(days)}d overdue` };
  if (days === 0) return { cls: "bg-red-lt", label: "Due today" };
  if (days <= 7) return { cls: "bg-yellow-lt", label: `Due in ${days}d` };
  return { cls: "bg-blue-lt", label: `Due ${formatDate(task.due_date)}` };
}

export function TaskList({ tasks, loading, onToggle, onDelete, pendingId }: TaskListProps) {
  if (loading) {
    return (
      <div className="card card-body text-muted">
        <span className="spinner-border spinner-border-sm me-2" role="status" />
        Loading tasks…
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <div className="empty">
        <div className="empty-icon">
          <IconInbox size={24} />
        </div>
        <p className="empty-title">No tasks yet</p>
        <p className="empty-subtitle text-muted">
          Deadlines found in an uploaded document appear here automatically.
        </p>
      </div>
    );
  }

  return (
    <div className="list-group">
      {tasks.map((task) => {
        const due = dueBadge(task);
        const completed = task.status === "completed";

        return (
          <div key={task.id} className="list-group-item">
            <div className="row align-items-start g-2">
              <div className="col-auto pt-1">
                <input
                  type="checkbox"
                  className="form-check-input m-0"
                  checked={completed}
                  disabled={pendingId === task.id}
                  onChange={() => onToggle(task)}
                  aria-label={completed ? `Reopen ${task.title}` : `Complete ${task.title}`}
                />
              </div>
              <div className="col">
                <span
                  className={`d-block ${completed ? "text-muted text-decoration-line-through" : "fw-medium"}`}
                >
                  {pendingId === task.id ? (
                    <span className="spinner-border spinner-border-sm me-2" role="status" />
                  ) : null}
                  {task.title}
                </span>
                <span className="d-flex flex-wrap align-items-center gap-2 mt-1">
                  <span className={`badge ${due.cls}`}>{due.label}</span>
                  {task.source_filename ? (
                    <span className="text-muted text-truncate" style={{ fontSize: 12, maxWidth: 180 }} title={task.source_filename}>
                      {task.source_filename}
                    </span>
                  ) : null}
                </span>
              </div>
              <div className="col-auto">
                <button
                  type="button"
                  title={`Delete ${task.title}`}
                  onClick={() => onDelete(task)}
                  className="btn btn-icon btn-sm btn-ghost-danger"
                >
                  <IconTrash size={16} />
                </button>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
