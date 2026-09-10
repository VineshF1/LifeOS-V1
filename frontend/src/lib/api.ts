/**
 * Typed API client. Attaches the JWT Bearer token and normalises every error
 * into an ApiError carrying the backend's own message.
 */

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/+$/, "") ?? "http://localhost:8000";

const TOKEN_KEY = "lifeos.token";

// Upload runs chunking, embedding and extraction; chat runs up to 4 agent turns.
const UPLOAD_TIMEOUT_MS = 180_000;
const CHAT_TIMEOUT_MS = 120_000;
const DEFAULT_TIMEOUT_MS = 30_000;

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

// --------------------------------------------------------------------------
// Types
// --------------------------------------------------------------------------

export interface UserOut {
  id: string;
  email: string;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user: UserOut;
}

export interface DocumentMetadata {
  category?: string;
  issuer?: string | null;
  identifier?: string | null;
  financial_amount?: number | null;
  currency?: string | null;
  created_date?: string | null;
  action_deadline?: string | null;
  action_description?: string | null;
  task_warranted?: boolean;
  extraction_error?: string;
  drafted_task_id?: string;
}

export interface DocumentOut {
  id: string;
  filename: string;
  category: string;
  status: "processing" | "ready" | "needs_review" | string;
  has_actionable_deadline: boolean;
  metadata: DocumentMetadata;
  created_at: string;
  chunk_count: number;
}

export interface UploadResponse {
  document: DocumentOut;
  message: string | null;
  warnings: string[];
}

export interface TaskOut {
  id: string;
  document_id: string | null;
  title: string;
  due_date: string | null;
  status: "pending" | "completed" | string;
  created_at: string;
  source_filename: string | null;
}

export interface Citation {
  chunk_id: string;
  filename: string;
  page_number: number;
  excerpt: string;
}

export interface ToolTrace {
  iteration: number;
  tool_name: string;
  arguments: Record<string, unknown>;
  result_summary: string;
  execution_time_ms: number;
}

export interface ChatResponse {
  answer: string;
  citations: Citation[];
  tool_trace: ToolTrace[];
  iterations: number;
}

export const CATEGORIES = [
  "Insurance",
  "Tax",
  "Vehicle",
  "Utility",
  "Warranty",
  "Rental",
  "General",
] as const;

// --------------------------------------------------------------------------
// Transport
// --------------------------------------------------------------------------

async function parseErrorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.message === "string") return body.message;
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail) && body.detail[0]?.msg) return String(body.detail[0].msg);
  } catch {
    // Body was not JSON (proxy error page, empty 502, ...).
  }
  return `Request failed (${response.status}).`;
}

interface RequestOptions extends RequestInit {
  timeoutMs?: number;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...init } = options;

  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers, signal: controller.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(504, "That took too long. Please retry in a moment.");
    }
    throw new ApiError(0, "Cannot reach the API. Is the backend running on " + API_BASE + "?");
  } finally {
    clearTimeout(timer);
  }

  if (response.status === 401) {
    clearToken();
    throw new ApiError(401, "Your session expired. Please sign in again.");
  }
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

// --------------------------------------------------------------------------
// Endpoints
// --------------------------------------------------------------------------

export const api = {
  signup(email: string, password: string) {
    return request<TokenResponse>("/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
  },

  login(email: string, password: string) {
    return request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
  },

  me() {
    return request<UserOut>("/auth/me");
  },

  listDocuments(category?: string) {
    const query = category && category !== "All" ? `?category=${encodeURIComponent(category)}` : "";
    return request<{ documents: DocumentOut[] }>(`/documents${query}`);
  },

  uploadDocument(file: File) {
    const form = new FormData();
    form.append("file", file);
    return request<UploadResponse>("/documents/upload", {
      method: "POST",
      body: form,
      timeoutMs: UPLOAD_TIMEOUT_MS,
    });
  },

  deleteDocument(id: string) {
    return request<void>(`/documents/${id}`, { method: "DELETE" });
  },

  chat(message: string, documentId?: string | null) {
    return request<ChatResponse>("/chat", {
      method: "POST",
      body: JSON.stringify({ message, document_id: documentId ?? null }),
      timeoutMs: CHAT_TIMEOUT_MS,
    });
  },

  listTasks(status?: string) {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return request<TaskOut[]>(`/tasks${query}`);
  },

  updateTaskStatus(id: string, status: "pending" | "completed") {
    return request<TaskOut>(`/tasks/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
  },

  async deleteTask(id: string) {
    await request<void>(`/tasks/${id}`, { method: "DELETE" });
  },
};
