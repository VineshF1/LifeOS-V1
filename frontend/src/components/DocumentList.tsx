"use client";

import { IconCalendarDue, IconFileText, IconTrash } from "@tabler/icons-react";
import { CATEGORIES, type DocumentOut } from "@/lib/api";
import { formatDate } from "@/lib/utils";

interface DocumentListProps {
  documents: DocumentOut[];
  loading: boolean;
  category: string;
  onCategoryChange: (category: string) => void;
  onSelect: (document: DocumentOut) => void;
  onDelete: (document: DocumentOut) => void;
  activeDocumentId: string | null;
}

function statusClass(status: string): string {
  if (status === "ready") return "bg-green-lt";
  if (status === "needs_review") return "bg-yellow-lt";
  return "bg-blue-lt";
}

export function DocumentList({
  documents,
  loading,
  category,
  onCategoryChange,
  onSelect,
  onDelete,
  activeDocumentId,
}: DocumentListProps) {
  const filters = ["All", ...CATEGORIES];

  return (
    <div>
      <div className="d-flex flex-wrap gap-1 mb-3" role="group" aria-label="Filter by category">
        {filters.map((filter) => (
          <button
            key={filter}
            type="button"
            onClick={() => onCategoryChange(filter)}
            className={`btn btn-sm ${category === filter ? "btn-primary" : ""}`}
          >
            {filter}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="card card-body text-muted">
          <span className="spinner-border spinner-border-sm me-2" role="status" />
          Loading documents…
        </div>
      ) : documents.length === 0 ? (
        <div className="empty">
          <div className="empty-icon">
            <IconFileText size={24} />
          </div>
          <p className="empty-title">No documents yet</p>
          <p className="empty-subtitle text-muted">Upload a PDF to get started.</p>
        </div>
      ) : (
        <div className="list-group">
          {documents.map((document) => {
            const deadline = document.metadata?.action_deadline ?? null;
            const amount = document.metadata?.financial_amount ?? null;
            const currency = document.metadata?.currency ?? "";
            const isActive = activeDocumentId === document.id;

            return (
              <div
                key={document.id}
                className={`list-group-item ${isActive ? "active" : ""}`}
                style={{ cursor: "pointer" }}
                onClick={() => onSelect(document)}
              >
                <div className="d-flex align-items-start gap-2">
                  <span className="avatar avatar-sm rounded bg-blue-lt flex-shrink-0">
                    <IconFileText size={16} />
                  </span>
                  <div className="flex-fill" style={{ minWidth: 0 }}>
                    <span className="fw-medium d-block text-truncate">{document.filename}</span>
                    <span className="text-muted d-block" style={{ fontSize: 12 }}>
                      {document.chunk_count} chunks · {formatDate(document.created_at)} ·{" "}
                      {document.category}
                      {amount !== null ? ` · ${currency} ${amount}` : ""}
                    </span>
                    {deadline ? (
                      <span
                        className="text-muted d-flex align-items-center gap-1"
                        style={{ fontSize: 12 }}
                      >
                        <IconCalendarDue size={13} />
                        {formatDate(deadline)}
                      </span>
                    ) : null}
                    <span className={`badge mt-1 ${statusClass(document.status)}`}>
                      {document.status}
                    </span>
                  </div>
                  <button
                    type="button"
                    title={`Delete ${document.filename}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      onDelete(document);
                    }}
                    className="btn btn-icon btn-sm btn-ghost-danger flex-shrink-0"
                  >
                    <IconTrash size={16} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
