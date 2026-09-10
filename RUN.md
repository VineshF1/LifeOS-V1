# LifeOS — Run Guide

> Verified on **Windows 11, Python 3.11.16, Node 22.23.2, Next 15.1.6, pgvector 0.8.6, branch `main`**. All `terminal` commands run in **bash (git-bash/MSYS)** — pass native tools `C:/...` forward-slash paths.

## Prerequisites

- Python 3.11+ (`python --version` → 3.11.x; on this host use `python`, not `python3`)
- Node 22+ (`node -v`, `npm -v`)
- Neon Serverless Postgres branch with `pgvector` + free NVIDIA API key (https://build.nvidia.com, `nvapi-...`)
- `pip`, `psql` optional (for manual checks)

## Setup

### 1. Clone & env

```bash
cd C:/Users/VINAY/OneDrive/Desktop/LifeOS
git checkout main
cp backend/.env.example backend/.env
# edit backend/.env:
# DATABASE_URL=postgresql://USER:***@ep-xxxx-pooler.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
# NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
# optional: NVIDIA_BASE_URL, LLM_MODEL=nvidia/nemotron-3-super-120b-a12b,
#           EMBEDDING_MODEL=nvidia/nemotron-3-embed-1b, NIM_TIMEOUT_SECONDS=15,
#           CHAT_TIMEOUT_SECONDS=30, EXTRACTION_TIMEOUT_SECONDS=30,
#           AGENT_MAX_ITERATIONS=3, AGENT_MAX_TOKENS=1024,
#           CORS_ORIGINS=http://localhost:3000

cp frontend/.env.local.example frontend/.env.local
# only if API is not http://localhost:8000:
# NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

`backend/.env` may be `postgres://` or `postgresql://`; `database.py:normalize_database_url` strips `channel_binding`/`sslmode` and sets `statement_cache_size=0` for PgBouncer — both pooled and direct hosts work.

### 2. Backend (isolated venv — required)

The host's hermes-agent venv (`pydantic 2.13.4 / openai 2.24.0 / starlette 1.3.1`) conflicts with LifeOS pins (`pydantic 2.10.4 / openai 1.59.7 / starlette 0.41.3 / fastapi 0.115.6`). Do **not** install globally.

```bash
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install --upgrade pip
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip check
# → No broken requirements found.
```

### 3. Database (one-time)

```bash
cd backend
.venv/Scripts/python.exe -m app.init_db
# Applying schema to ep-xxxx/...
# pgvector 0.8.6
# Tables: audit_logs, document_chunks, documents, tasks, users
# Schema applied successfully.
```

Creates `vector` + `uuid-ossp`, 5 tables (`vector(2048)` → `halfvec(2048)` HNSW `halfvec_cosine_ops`; `vector` HNSW caps at 2000 dims), `ENABLE/FORCE ROW LEVEL SECURITY`, and 4 `tenant_isolation_*` policies (`current_setting('app.current_user_id', true)` fail-closed). Tasks cascade on document delete. Idempotent — safe to re-run.

### 4. Frontend (Tabler.io, no shadcn)

```bash
cd frontend
npm install   # @tabler/core 1.5.1 + @tabler/icons-react 3.46.0 included
npm run typecheck   # tsc --noEmit — must exit 0
```

> ⚠️ **Never run `npm run build` while `npm run dev` serves the same directory** — build output corrupts dev's `.next` (pages 500, then every JS/CSS chunk 404s). Stop dev first, build, restart. If the page ever renders unstyled/huge: kill all port-3000 holders, `rm -rf .next`, restart dev.

No ESLint config shipped — `next build` passes without it. `npm run lint` will prompt interactively; ignore for the demo.

## Run

**Terminal 1 — Backend (8000):**
```bash
cd backend
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --log-level info
# Database ready (pgvector 0.8.6).
# Application startup complete.
# Uvicorn running on http://127.0.0.1:8000
```

(If `.env` is still placeholder you'll see `Database unreachable ...` instead — app still serves `/health`; uploads will just warn.)

**Terminal 2 — Frontend (3000):**
```bash
cd frontend
npm run dev -- --port 3000 --hostname 127.0.0.1
# Local: http://127.0.0.1:3000
```

Open **http://localhost:3000** → *Create account* → dashboard. Production: `npm run build && npm run start` (frontend, dev stopped) and `uvicorn app.main:app --host 0.0.0.0 --port 8000` (backend, no `--reload`).

## API Examples

```bash
BASE=http://127.0.0.1:8000

# health
curl $BASE/health | python -m json.tool
# → {status: ok, llm_model: nemotron-3-super…, embedding_dim: 2048, agent_max_iterations: 3, agents_configured: true}

# signup + login
curl -X POST $BASE/auth/signup -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"TestPass123!"}' | python -m json.tool
TOKEN=$(curl -s -X POST $BASE/auth/login -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"TestPass123!"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# upload PDF (≤25 MB, must start with %PDF-)
curl -X POST $BASE/documents/upload -H "Authorization: Bearer ***" \
  -F file=@invoice.pdf              # → {document: {status: ready, chunk_count: 1}, warnings: []}
# scanned image-only PDF → 201 {status: needs_review, metadata: {extraction_error: scanned_no_text}}

# list / filter
curl -H "Authorization: Bearer ***" "$BASE/documents?category=Utility" | python -m json.tool
curl -H "Authorization: Bearer ***" "$BASE/tasks?status=pending" | python -m json.tool

# toggle task
curl -X PATCH $BASE/tasks/<task_id> -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" -d '{"status":"completed"}'

# delete task (also removes pre-existing orphans)
curl -X DELETE $BASE/tasks/<task_id> -H "Authorization: Bearer ***" -w "%{http_code}\n"
# → 204

# delete document (its tasks are deleted too)
curl -X DELETE $BASE/documents/<doc_id> -H "Authorization: Bearer ***" -w "%{http_code}\n"
# → 204

# agentic chat — single-doc scope + citation
curl -X POST $BASE/chat -H "Authorization: Bearer ***" -H "Content-Type: application/json" \
  -d '{"message":"When does my insurance expire?","document_id":"<uuid>"}' | python -m json.tool
# → {answer: "... [Source: policy.pdf, Page: 1, Excerpt: \"...\"]", citations: [...], tool_trace: [{tool_name: search_documents, ...}]}

# cross-doc chat
curl -X POST $BASE/chat -H "Authorization: Bearer ***" -H "Content-Type: application/json" \
  -d '{"message":"Which bills are due next month?"}' | python -m json.tool
```

## Usage

1. Start backend (`:8000`) and frontend (`:3000`).
2. Create an account at `http://localhost:3000` (or `/login`).
3. Drag a searchable digital PDF (bills, policies, warranties — scanned image-only PDFs return `needs_review` with guidance, not an error) onto the **Upload** card — `ready` in ~15–25 s with an auto-drafted task when a deadline is found.
4. Check **Documents** (wrapping category filters, scrollable list, deadline chips) and **Tasks** (auto-drafted, soonest first; reload ↻ and per-task delete available).
5. Ask in **Ask your documents** — pick *All documents* or a single file in *Scope*, `+` starts a new conversation. Answers carry expandable `[Source: file, pN]` citation chips with verbatim excerpts.
6. Deleting a document deletes its tasks; stray tasks can be removed with their trash icon.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `ModuleNotFoundError: No module named 'app'` | Ran `uvicorn` from wrong cwd. `cd backend` first, or `uvicorn app.main:app --app-dir backend`. Verified form: `cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000`. |
| `AssertionError: Status code 204 must not have a response body` | On host venv (`fastapi 0.133.1`). Use `backend/.venv` (`0.115.6`) where deletes return `Response(204)`. |
| `Database unreachable at startup` | `.env` still placeholder. Fill `DATABASE_URL` and re-run `python -m app.init_db`. App still serves `/health` without DB. |
| `AI inference timed out` (504 on chat) | NIM load spike. Chat turns now get 30 s + 1 retry — just retry. Check `agents_configured` in `/health` and `NVIDIA_API_KEY`. |
| `AI extraction timed out` warning on upload | Extraction gets 30 s + 1 retry; worst case the doc stays searchable as `needs_review`. Re-upload — 6-page bills complete in ~13 s when NIM is healthy. |
| Scanned PDF → `needs_review` + `scanned_no_text` | By design: no text layer, PyMuPDF has no OCR. Upload a searchable PDF (export the scan with OCR first). |
| Deleted document's task still listed | Pre-fix orphans (`document_id` already NULL) can't cascade — use the task's trash icon (`DELETE /tasks/{id}`). New deletes cascade automatically. |
| Page renders unstyled/huge, chunks 404, `/login` 500 | `.next` corrupted by building while dev ran. Kill port-3000 holders (`netstat -ano \| grep :3000`), `rm -rf .next`, restart `npm run dev`. Then hard-refresh (`Ctrl+Shift+R`). |
| `EADDRINUSE 127.0.0.1:3000` on `npm run dev` | Stale Next worker. Kill every LISTENING PID on :3000, then start dev. |
| `pgvector: column cannot have more than 2000 dimensions for hnsw` | Don't rewrite `schema.sql` to `vector_cosine_ops` on `vector(2048)`. Repo already uses `halfvec(2048)` (`halfvec_cosine_ops`). |
| `npm run lint` prompts `How would you like to configure ESLint?` | No ESLint config shipped — intentional. `next build` passes without it. |
| `pip check` reports conflicts after global install | You polluted the hermes-agent venv. Re-create `backend/.venv`; restore hermes with `pip install "openai==2.24.0" "pydantic==2.13.4" "PyJWT[crypto]==2.13.0" "starlette==1.3.1" "fastapi==0.133.1"` if needed. |
| Frontend `Cannot reach the API` | `NEXT_PUBLIC_API_BASE_URL` mismatch — check `frontend/.env.local` and that backend is on 8000. |
| Extraction returns `action_deadline: null` | Model returned partial JSON — repo now has example + `_enrich_from_text` regex fallback for `Due Date: YYYY-MM-DD`, `$amount`, `Issuer:` so the deadline is not lost. |

## Useful One-Liners

```bash
# LOC
pygount --format=summary --folders-to-skip=".git,node_modules,venv,.venv,__pycache__,.cache,dist,build,.next" .

# regenerate JWT secret
python -c "import secrets; print(secrets.token_urlsafe(48))"

# offline ingestion smoke (no DB/NIM)
.venv/Scripts/python.exe -c "import fitz, io; from app.ingestion import extract_pages, chunk_pages; d=fitz.open(); d.new_page().insert_text((50,50),'hello'); b=io.BytesIO(); d.save(b); print(len(chunk_pages(extract_pages(b.getvalue()))))"

# verify citation rendering
.venv/Scripts/python.exe -c "from app.routers.chat import render_citations; from uuid import uuid4; cid=str(uuid4()); print(render_citations(f'hi [REF: {cid}]', {cid.lower(): {'content':' '.join(['w']*30), 'filename':'a.pdf','page_number':1}}))"

# live embedding check (needs NVIDIA_API_KEY)
.venv/Scripts/python.exe -c "import asyncio; from app.nim import embed_texts; v=asyncio.run(embed_texts(['smoke'], input_type='passage')); print(len(v), len(v[0]))"
# → 1 2048
```
