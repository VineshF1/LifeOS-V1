"use client";

import { IconRobot, IconSend, IconUser } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError, api, type DocumentOut, type ToolTrace } from "@/lib/api";

const SOURCE_TAG = /\[Source:\s*(.*?),\s*Page:\s*(\d+),\s*Excerpt:\s*"([^"]*)"\]/g;

function CitationChip({
  filename,
  page,
  excerpt,
}: {
  filename: string;
  page: string;
  excerpt: string;
}) {
  const short = filename.length > 28 ? `${filename.slice(0, 28)}…` : filename;

  return (
    <details className="d-inline-block align-middle mx-1">
      <summary className="badge bg-blue-lt" style={{ cursor: "pointer" }}>
        {short} · p{page}
      </summary>
      <div className="card card-body citation-pop mt-1 p-2">
        <div className="fw-medium" style={{ fontSize: 12 }}>
          {filename} — page {page}
        </div>
        <div className="text-muted fst-italic" style={{ fontSize: 12 }}>
          “{excerpt}”
        </div>
      </div>
    </details>
  );
}

function renderWithCitations(text: string) {
  const nodes: ReactNode[] = [];
  const regex = new RegExp(SOURCE_TAG.source, "g");
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) nodes.push(text.slice(lastIndex, match.index));
    nodes.push(<CitationChip key={`cite-${key++}`} filename={match[1]} page={match[2]} excerpt={match[3]} />);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return nodes;
}

type Message =
  | { id: number; role: "user"; content: string }
  | {
      id: number;
      role: "assistant";
      content: string;
      toolTrace: ToolTrace[];
      iterations: number;
    };

interface ChatPaneProps {
  documents: DocumentOut[];
  scopeDocumentId: string | null;
  onScopeChange: (documentId: string | null) => void;
  onSessionExpired: () => void;
  onTasksChanged: () => void;
}

export function ChatPane({
  documents,
  scopeDocumentId,
  onScopeChange,
  onSessionExpired,
  onTasksChanged,
}: ChatPaneProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const nextId = useRef(0);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  async function send() {
    const question = input.trim();
    if (!question || busy) return;

    setError(null);
    setInput("");
    setMessages((previous) => [...previous, { id: nextId.current++, role: "user", content: question }]);
    setBusy(true);

    try {
      const response = await api.chat(question, scopeDocumentId);
      setMessages((previous) => [
        ...previous,
        {
          id: nextId.current++,
          role: "assistant",
          content: response.answer,
          toolTrace: response.tool_trace ?? [],
          iterations: response.iterations ?? 0,
        },
      ]);
      if ((response.tool_trace ?? []).some((entry) => entry.tool_name === "create_task")) {
        onTasksChanged();
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onSessionExpired();
        return;
      }
      setError(err instanceof ApiError ? err.message : "The agent could not answer that.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="d-flex flex-column h-100">
      <div className="d-flex align-items-center gap-2 p-3 border-bottom">
        <label htmlFor="chat-scope" className="form-label mb-0 text-muted">
          Scope
        </label>
        <select
          id="chat-scope"
          value={scopeDocumentId ?? ""}
          onChange={(event) => onScopeChange(event.target.value || null)}
          className="form-select form-select-sm"
        >
          <option value="">All documents</option>
          {documents.map((document) => (
            <option key={document.id} value={document.id}>
              {document.filename}
            </option>
          ))}
        </select>
      </div>

      <div ref={scrollRef} className="flex-fill p-3 chat-scroll" style={{ overflowY: "auto", minHeight: 320 }}>
        {messages.length === 0 ? (
          <div className="empty">
            <div className="empty-icon">
              <IconRobot size={24} />
            </div>
            <p className="empty-title">Ask about your documents</p>
            <p className="empty-subtitle text-muted">The agent will choose tools and cite sources.</p>
            <div className="empty-action">
              {[
                "When does my car insurance expire?",
                "Which bills are due next month?",
                "What does my rental agreement say about the deposit?",
              ].map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => setInput(example)}
                  className="btn btn-sm mb-1 me-1"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="d-flex flex-column gap-3">
            {messages.map((message) =>
              message.role === "user" ? (
                <div key={message.id} className="d-flex justify-content-end gap-2">
                  <div className="card card-body p-2 px-3 bg-primary text-white" style={{ maxWidth: "85%" }}>
                    {message.content}
                  </div>
                  <span className="avatar avatar-xs rounded-circle bg-muted-lt">
                    <IconUser size={14} />
                  </span>
                </div>
              ) : (
                <div key={message.id} className="d-flex gap-2">
                  <span className="avatar avatar-xs rounded-circle bg-blue-lt">
                    <IconRobot size={14} />
                  </span>
                  <div style={{ maxWidth: "92%" }} className="min-w-0">
                    <div className="card card-body p-2 px-3">
                      <span className="prose-answer">{renderWithCitations(message.content)}</span>
                    </div>
                  </div>
                </div>
              ),
            )}
          </div>
        )}

        {busy ? (
          <div className="card card-body p-2 px-3 mt-3 text-muted d-flex flex-row align-items-center gap-2">
            <span className="spinner-border spinner-border-sm" role="status" />
            The agent is choosing tools and reading your documents…
          </div>
        ) : null}
      </div>

      {error ? (
        <div className="alert alert-danger mx-3 mb-2" role="alert">
          {error}
        </div>
      ) : null}

      <div className="p-3 border-top">
        <div className="position-relative">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
            rows={3}
            placeholder="Ask a question about your documents…"
            disabled={busy}
            className="form-control"
            style={{ borderRadius: "1rem", paddingRight: "3.25rem", resize: "vertical" }}
          />
          <button
            type="button"
            onClick={() => void send()}
            disabled={busy || !input.trim()}
            className="btn btn-primary btn-icon rounded-circle position-absolute"
            style={{ bottom: "0.6rem", right: "0.6rem" }}
            aria-label="Send"
          >
            {busy ? (
              <span className="spinner-border spinner-border-sm" role="status" />
            ) : (
              <IconSend size={16} />
            )}
          </button>
        </div>
        <p className="text-muted mb-0 mt-1" style={{ fontSize: 12 }}>
          Enter to send · Shift+Enter for a new line
        </p>
      </div>
    </div>
  );
}
