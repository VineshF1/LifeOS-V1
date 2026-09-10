"use client";

import {
  IconFileText,
  IconLogout,
  IconMessageChatbot,
  IconPlus,
  IconRefresh,
} from "@tabler/icons-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ChatPane } from "@/components/ChatPane";
import { DocumentList } from "@/components/DocumentList";
import { Dropzone } from "@/components/Dropzone";
import { TaskList } from "@/components/TaskList";
import {
  ApiError,
  api,
  clearToken,
  getToken,
  type DocumentOut,
  type TaskOut,
  type UploadResponse,
  type UserOut,
} from "@/lib/api";

export default function DashboardPage() {
  const router = useRouter();

  const [user, setUser] = useState<UserOut | null>(null);
  const [ready, setReady] = useState(false);
  const [documents, setDocuments] = useState<DocumentOut[]>([]);
  const [documentsLoading, setDocumentsLoading] = useState(true);
  const [tasks, setTasks] = useState<TaskOut[]>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [category, setCategory] = useState("All");
  const [scopeDocumentId, setScopeDocumentId] = useState<string | null>(null);
  const [pendingTaskId, setPendingTaskId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [chatKey, setChatKey] = useState(0);

  const signOut = useCallback(() => {
    clearToken();
    router.replace("/login");
  }, [router]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    let cancelled = false;
    api
      .me()
      .then((p) => {
        if (!cancelled) {
          setUser(p);
          setReady(true);
        }
      })
      .catch(() => {
        if (!cancelled) signOut();
      });
    return () => {
      cancelled = true;
    };
  }, [router, signOut]);

  const loadDocuments = useCallback(async () => {
    setDocumentsLoading(true);
    try {
      const r = await api.listDocuments();
      setDocuments(r.documents);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) signOut();
      else setNotice(e instanceof ApiError ? e.message : "Could not load documents.");
    } finally {
      setDocumentsLoading(false);
    }
  }, [signOut]);

  const loadTasks = useCallback(async () => {
    setTasksLoading(true);
    try {
      setTasks(await api.listTasks());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) signOut();
      else setNotice(e instanceof ApiError ? e.message : "Could not load tasks.");
    } finally {
      setTasksLoading(false);
    }
  }, [signOut]);

  useEffect(() => {
    if (ready) {
      void loadDocuments();
      void loadTasks();
    }
  }, [ready, loadDocuments, loadTasks]);

  async function toggleTask(task: TaskOut) {
    setPendingTaskId(task.id);
    try {
      const next = task.status === "completed" ? "pending" : "completed";
      const updated = await api.updateTaskStatus(task.id, next);
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "Could not update task.");
    } finally {
      setPendingTaskId(null);
    }
  }

  async function deleteTask(task: TaskOut) {
    if (!window.confirm(`Delete task "${task.title}"?`)) return;
    try {
      await api.deleteTask(task.id);
      setTasks((prev) => prev.filter((t) => t.id !== task.id));
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "Could not delete task.");
    }
  }

  async function deleteDocument(document: DocumentOut) {
    if (!window.confirm(`Delete ${document.filename}?`)) return;
    try {
      await api.deleteDocument(document.id);
      setDocuments((prev) => prev.filter((d) => d.id !== document.id));
      if (scopeDocumentId === document.id) setScopeDocumentId(null);
      // The backend deletes the document's tasks too — reload so orphans vanish.
      void loadTasks();
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "Could not delete document.");
    }
  }

  function handleUploaded(result: UploadResponse) {
    setDocuments((prev) => [result.document, ...prev]);
    if (result.warnings.length > 0) {
      setNotice(`Saved with warnings: ${result.warnings.join("; ")}`);
    } else if (result.message) {
      setNotice(result.message);
    }
    void loadTasks();
  }

  if (!ready) {
    return (
      <div className="page page-center">
        <div className="container container-tight py-4 text-center">
          <span className="spinner-border" role="status" />
          <p className="text-muted mt-2 mb-0">Loading LifeOS…</p>
        </div>
      </div>
    );
  }

  const filteredDocuments =
    category === "All" ? documents : documents.filter((d) => d.category === category);
  const pendingCount = tasks.filter((t) => t.status === "pending").length;
  const readyCount = documents.filter((d) => d.status === "ready").length;

  return (
    <div className="page">
      <header className="navbar navbar-expand-md d-print-none">
        <div className="container-xl">
          <h1 className="navbar-brand navbar-brand-autodark d-none-navbar-horizontal pe-0 pe-md-3">
            <span className="avatar avatar-sm rounded me-2" style={{ background: "var(--tblr-primary)" }}>
              <IconFileText size={18} color="#fff" />
            </span>
            LifeOS Agent
          </h1>
          <div className="navbar-nav flex-row order-md-last">
            <div className="nav-item d-none d-md-flex me-3">
              <span className="text-muted">{user?.email}</span>
            </div>
            <div className="nav-item">
              <button type="button" onClick={signOut} className="btn btn-sm btn-ghost-danger">
                <IconLogout size={16} className="me-1" />
                Sign out
              </button>
            </div>
          </div>
        </div>
      </header>

      <div className="page-wrapper">
        <div className="page-header d-print-none">
          <div className="container-xl">
            <div className="row g-2 align-items-center">
              <div className="col">
                <div className="page-pretitle">Personal document agent</div>
                <h2 className="page-title">Your documents, turned into action</h2>
                <p className="text-muted mb-0">
                  Upload a PDF. It gets chunked, embedded and extracted. Ask questions — the
                  agent chooses tools and cites sources.
                </p>
              </div>
              <div className="col-auto ms-auto d-print-none">
                <div className="btn-list">
                  <span className="badge bg-blue-lt">{documents.length} documents</span>
                  <span className="badge bg-green-lt">{readyCount} ready</span>
                  <span className="badge bg-yellow-lt">{pendingCount} pending tasks</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className="page-body">
          <div className="container-xl">
            {notice ? (
              <div className="alert alert-warning alert-dismissible" role="alert">
                {notice}
                <button
                  type="button"
                  className="btn-close"
                  aria-label="Dismiss"
                  onClick={() => setNotice(null)}
                />
              </div>
            ) : null}

            <div className="row row-deck row-cards">
              <div className="col-lg-4 d-flex flex-column gap-3">
                <div className="card">
                  <div className="card-header">
                    <h3 className="card-title">Upload</h3>
                  </div>
                  <div className="card-body">
                    <Dropzone onUploaded={handleUploaded} />
                  </div>
                </div>

                <div className="card">
                  <div className="card-header">
                    <h3 className="card-title text-truncate" style={{ minWidth: 0 }}>
                      Documents · {documents.length}
                    </h3>
                    <div
                      className="card-actions text-muted flex-shrink-0 ms-2"
                      style={{ fontSize: 12 }}
                    >
                      {documentsLoading ? "loading…" : `${filteredDocuments.length} shown`}
                    </div>
                  </div>
                  <div className="card-body">
                    <div style={{ maxHeight: 420, overflowY: "auto" }} className="chat-scroll">
                      <DocumentList
                        documents={filteredDocuments}
                        loading={documentsLoading}
                        category={category}
                        onCategoryChange={setCategory}
                        onSelect={(doc) =>
                          setScopeDocumentId(doc.id === scopeDocumentId ? null : doc.id)
                        }
                        onDelete={(doc) => void deleteDocument(doc)}
                        activeDocumentId={scopeDocumentId}
                      />
                    </div>
                  </div>
                </div>
              </div>

              <div className="col-lg-5">
                <div className="card d-flex flex-column" style={{ minHeight: 560 }}>
                  <div className="card-header">
                    <span className="avatar avatar-xs rounded bg-blue-lt me-2">
                      <IconMessageChatbot size={14} />
                    </span>
                    <h3 className="card-title">Ask your documents</h3>
                    <div className="card-actions">
                      <button
                        type="button"
                        title="Start a new conversation"
                        aria-label="Start a new conversation"
                        onClick={() => setChatKey((key) => key + 1)}
                        className="btn btn-icon btn-sm"
                      >
                        <IconPlus size={16} />
                      </button>
                    </div>
                  </div>
                  <div
                    className="flex-fill"
                    style={{ height: "clamp(480px, calc(100vh - 320px), 720px)" }}
                  >
                    <ChatPane
                      key={chatKey}
                      documents={documents}
                      scopeDocumentId={scopeDocumentId}
                      onScopeChange={setScopeDocumentId}
                      onSessionExpired={signOut}
                      onTasksChanged={() => void loadTasks()}
                    />
                  </div>
                </div>
              </div>

              <div className="col-lg-3 d-flex flex-column gap-3">
                <div className="card">
                  <div className="card-header">
                    <h3 className="card-title text-truncate" style={{ minWidth: 0 }}>
                      Tasks · {pendingCount} pending
                    </h3>
                    <div className="card-actions">
                      <button
                        type="button"
                        title="Reload tasks"
                        aria-label="Reload tasks"
                        onClick={() => void loadTasks()}
                        disabled={tasksLoading}
                        className="btn btn-icon btn-sm"
                      >
                        <IconRefresh size={16} />
                      </button>
                    </div>
                  </div>
                  <div className="card-body">
                    <TaskList
                      tasks={tasks}
                      loading={tasksLoading}
                      onToggle={(task) => void toggleTask(task)}
                      onDelete={(task) => void deleteTask(task)}
                      pendingId={pendingTaskId}
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
