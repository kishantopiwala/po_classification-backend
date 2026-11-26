# PO classification API

Upload a PDF, receive a job ID, then watch extraction progress and the final JSON
through server-sent events (SSE). Requires Poppler (`pdfinfo`, `pdftoppm`), Python
3.14+, and a running Ollama server with a vision model (default `gemma4:e4b`).

## Run

From `backend/`:

```bash
uv sync --locked
cp .env.example .env
uv run fastapi run --host 127.0.0.1 --port 8000 --workers 1
```

`fastapi run` also works when the backend virtual environment is activated.
The default `main.py` entry point loads `backend/.env` automatically, preserving
existing environment variables. `uv run uvicorn main:app ...` also works.
When using `app.main:app` directly, keep Uvicorn's `--env-file .env` option.
API documentation: http://127.0.0.1:8000/docs.

Use one worker. Jobs and event history live in memory, so they disappear on process
restart or code reload. One extraction runs at a time; closing the SSE connection
does not cancel the job. The local API has no authentication; keep it bound to
localhost. For shared deployment, add authentication/job ownership and replace
in-memory jobs with durable storage and an external worker before using multiple
workers or replicas.

## Postman

Import [postman_collection.json](postman_collection.json).

1. Set **Settings → General → Request timeout** to `0` for streaming tests.
2. Open **1. Upload PDF**. Under **Body → form-data**, choose your PDF for `file`.
   Leave `Content-Type` unset so Postman adds the multipart boundary. Send.
3. A `202` response automatically saves `job_id` in collection variables.
4. Send **2. Watch events**. Observe progress and heartbeat events. The
   `completed` event contains the final JSON; `failed` contains an error. Both close
   the stream.
5. Use **3. Get status or result** at any time, including after disconnecting.
6. For reconnect testing, set `last_event_id` to the last received event `id`, then
   send **4. Resume events**. Heartbeats have no IDs and are not replayed.

The stream replays stored history, so a fast job still produces its result when
you subscribe later. A stream opened after the acknowledged terminal event closes
without repeating that event. Unknown/expired job IDs return `404`; invalid or
out-of-range event IDs return `400`.

## API contract

### General chat

`POST /api/v1/chat` tests the configured Ollama model without uploading a PDF.
Send a nonempty `messages` array with `system`, `user`, or `assistant` roles and
nonblank text. Include previous messages on each request to continue a conversation;
the server does not store chat history. Uses `OLLAMA_HOST`, `OLLAMA_MODEL`, and
`OLLAMA_TIMEOUT`, and returns one completed response (no streaming).

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello! What can you do?"}]}'
```

Example response:

```json
{"model":"gemma4:e4b","message":{"role":"assistant","content":"Hello! How can I help?"},"done":true}
```

Use **General chat** in the Postman collection for the same request. Invalid input
returns `422`; unavailable Ollama, model errors, and incomplete/invalid model
responses return `502`. Detailed errors are logged server-side.

### PDF extraction

`POST /api/v1/extractions`: multipart field `file`, one PDF. Saves the upload to a
generated temporary path, queues the job, and returns immediately after upload:

```json
{
  "job_id": "uuid",
  "status": "queued",
  "events_url": "/api/v1/extractions/uuid/events",
  "status_url": "/api/v1/extractions/uuid"
}
```

`GET /api/v1/extractions/{job_id}` returns `job_id`, `filename`, `model`, `status`,
`total_pages`, `result`, and `error`. Status is `queued`, `processing`, `completed`,
or `failed`. Result/error remain null until available. Internal server paths are
excluded from API results.

`GET /api/v1/extractions/{job_id}/events`: `text/event-stream`, optionally with
`Last-Event-ID`. Events use JSON data and a blank line delimiter:

```text
id: 3
event: progress
data: {"job_id":"uuid","status":"processing","stage":"rendering","page":1,"total_pages":2}

event: heartbeat
data: {"status":"processing"}

id: 7
event: completed
data: {"job_id":"uuid","status":"completed","filename":"purchase.pdf","model":"gemma4:e4b","total_pages":2,"result":{"purchase_orders":[]},"error":null}

```

Progress stages are `starting`, `validating`, `validated`, `rendering`, `inference`,
and `parsing`. Page numbers describe rendering progress. Ollama returns one final
JSON object; inference has no estimated percentage or token stream. Extraction
failures are terminal SSE `failed` events, since an open stream already has HTTP
status `200`. Detailed diagnostics are logged server-side.

Pre-stream errors: `400` empty/bad PDF header, `413` file too large, `415` wrong
extension/media type, `422` missing file, `503` job capacity reached (retry later).
Encrypted/corrupt PDFs with a PDF header, page-limit violations, and Ollama errors
are reported as failed jobs after acceptance. Temporary uploads are deleted after
completion or failure. With PostgreSQL configured, a persistent source copy is
retained for successfully stored documents. Partial/rejected uploads are deleted
immediately. Queued uploads are
removed on graceful shutdown; an active extraction is allowed to finish. Forced
termination can leave temporary uploads behind.

## Configuration

See [.env.example](.env.example). Defaults:

| Setting | Default |
|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` |
| `OLLAMA_MODEL` | `gemma4:e4b` |
| `OLLAMA_TIMEOUT` | 600 seconds per Ollama request |
| `OLLAMA_NUM_CTX` | 16384 tokens for PDF extraction and PO chat context |
| `OLLAMA_NUM_PREDICT` | 8192 maximum output tokens for PDF extraction and PO chat; must be less than context |
| `PDF_DPI` | 150 |
| `MAX_UPLOAD_BYTES` | 20971520 (20 MiB) |
| `MAX_PDF_PAGES` | 20 |
| `JOB_QUEUE_SIZE` | 10 waiting/reserved uploads, plus one active job |
| `MAX_RETAINED_JOBS` | 100, including active jobs |
| `JOB_RETENTION_SECONDS` | 3600 after job completion/failure |
| `SSE_HEARTBEAT_SECONDS` | 15 |
| `UPLOAD_DIR` | OS temporary directory |

Retention expires results and event history; file cleanup happens as soon as the
job finishes. FastAPI parses multipart uploads before handler validation, so the
file-size limit is checked after multipart parsing. For an exposed deployment,
also cap incoming request bodies at the proxy/server boundary.

SSE uses `Cache-Control: no-cache` and `X-Accel-Buffering: no`. Disable proxy
buffering/compression for this route and configure idle timeouts above the
heartbeat interval. Heartbeats do not override hard hosting request-duration
limits or the independent Ollama request timeout.

## Code and checks

`app/extraction/` contains routes, schemas, job management, SSE encoding, the
extraction service, and editable `prompts.py`. `app/core/config.py` holds settings.
The root CLI remains available as `python extract_pdf.py document.pdf`.

From the repository root:

```bash
backend/.venv/bin/python -m unittest -v test_extract_pdf backend.tests.test_extraction_api backend.tests.test_chat_api
backend/.venv/bin/python -m backend.tests.live_sse_check
LIVE_INFERENCE_SECONDS=125 backend/.venv/bin/python -m backend.tests.live_sse_check
```

Tests use actual Poppler rendering and a local Ollama HTTP fixture. The live check
runs Uvicorn over real HTTP, disconnects/reconnects the event stream, checks
heartbeats and final JSON, and verifies upload cleanup. No downloaded model is
required for these checks.

## Persistent POs and MCP chat

The backend now supports PostgreSQL storage using async SQLAlchemy/asyncpg,
Alembic migrations, and read-only MCP tools mounted in the same FastAPI server.
The chat backend acts
as an MCP client and sends tool definitions and tool results to Ollama.

### Setup

From `backend/`, install dependencies with `uv sync --locked`. Update `.env`
using `.env.example`:

- `DATABASE_URL`: `postgresql+asyncpg://user:password@host:5432/database`.
  Create the database and user beforehand. URL-encode special characters in
  credentials. Alembic and the application use the same URL.
- `DOCUMENT_STORAGE_DIR`: persistent PDF directory. Relative paths resolve from
  `backend/`, regardless of the current working directory. Back up this directory
  together with the database.
- `PO_CHAT_MODEL`: an installed Ollama model reporting `tools` capability.
  `OLLAMA_MODEL` continues to select the PDF vision model. Both use `OLLAMA_HOST`.
- `MCP_PATH`: defaults to `/mcp`, served on the API's port 8000.
- `MCP_SERVER_URL`: defaults to `http://127.0.0.1:8000/mcp`. Update existing
  `.env` files that still point to port 8001. If changing the API port or MCP
  path, update this URL to match. `MCP_HOST` and `MCP_PORT` only apply to the
  optional standalone MCP entry point.

Apply migrations explicitly, then start one backend process:

```bash
uv run alembic upgrade head
uv run uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000 --workers 1
```

The API is available at `http://127.0.0.1:8000/api/v1/...`, Swagger at `/docs`,
and MCP Streamable HTTP at `http://127.0.0.1:8000/mcp`. The API manages the MCP
transport lifespan and shares its PO service/database with the tools.

Alembic loads `backend/.env`; environment variables take precedence.
API startup uses Uvicorn's `--env-file`. Schema creation is exclusively
through Alembic, never `create_all` at application startup.

When `DATABASE_URL` is absent, the original temporary extraction/general-chat
mode still works; PO endpoints return `503`. With the database configured,
extraction validates dates and decimal fields and commits the document, its POs,
and items before emitting `completed`. The response includes `document_id`.
Identical file bytes reuse the existing document without duplicating POs.
Temporary extraction uploads are still deleted; persistent source copies remain.
Previously uploaded/expired jobs are not automatically imported; upload the PDFs
again to populate the database.

### Import PDFs from a folder

Put your PDF files in `backend/po_to_store/`, then run from `backend/`:

```bash
uv run alembic upgrade head
uv run python store_pos.py
```

The script loads `backend/.env` and talks directly to Ollama and PostgreSQL;
the API server does not need to be running. It extracts each PDF and uses the
same Pydantic validation and transactional storage as API uploads. Original PDFs
are copied into `DOCUMENT_STORAGE_DIR`; source files stay in `po_to_store`.
Already stored file hashes are skipped before inference. Failures are reported
per file and processing continues; the final exit code is nonzero if any file
failed. Only PDFs immediately inside the folder are processed (including `.PDF`).

To use another folder: `uv run python store_pos.py --folder /path/to/pdfs`.

### Search and chat

```bash
curl 'http://127.0.0.1:8000/api/v1/purchase-orders?item_query=Honeycrisp&date_from=2024-04-01&date_before=2024-05-01'

curl -X POST http://127.0.0.1:8000/api/v1/po-chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Find Honeycrisp bags ordered in April 2024"}'
```

PO chat returns `session_id`, `answer`, and `tool_results`, including structured
rows and source URLs. Send the returned `session_id` with the next `message` for
follow-ups. The last 20 stored messages and their retrieved context are supplied
to the model. Failed turns are not recorded; starting a new conversation can
leave an empty session if inference fails. Overlapping turns for an existing
session return `409` within the supported single-worker deployment.

Endpoints:

| Endpoint | Behavior |
|---|---|
| `GET /api/v1/purchase-orders` | Filtered, paginated PO headers |
| `POST /api/v1/purchase-orders/search` | Same search using a JSON filter body |
| `GET /api/v1/purchase-orders/{id}` | Header, line items and source URL |
| `GET /api/v1/documents/{id}/download` | Original stored PDF |
| `POST /api/v1/po-chat` | MCP-backed PO chat |

MCP tools: `search_purchase_orders`, `get_purchase_order`, `search_po_items`,
and `summarize_purchase_orders`. Search tools accept a `filters` object;
`get_purchase_order` accepts the internal `po_id` UUID returned by search.
The MCP transport is Streamable HTTP with stateless requests.

Search fields are documented in `/docs`. PO/item numbers use exact matching;
company names and item descriptions use case-insensitive literal substring
matching. `date_from` is inclusive and `date_before` exclusive. `date_field`
selects issue date or estimated shipping date. Pagination defaults to 20 rows,
capped by `PO_SEARCH_MAX_LIMIT` (at most 100). Monetary values and quantities are
returned as decimal strings to preserve precision. Unit and price basis are
preserved without conversion. Counts and totals cover all matching POs and are
grouped by currency; item filters select POs, and totals remain whole-PO totals.

### Current boundaries

This is a local, single-organization backend. There is no authentication or
per-user/organization access control yet: everyone who can reach these services
can access stored POs, PDFs and chat sessions. Bind both services to loopback;
add authentication and authorization before shared deployment. The MCP tools
expose defined read operations, never arbitrary SQL.

Revised PDFs are preserved as separate POs with `needs_review` status. Search
and aggregation exclude revised documents by default and warn that original
versions may be outdated. Use `include_revisions=true` to inspect all versions;
there is no automatic superseding or review workflow yet. Extraction field
accuracy still requires review. Cross-currency or cross-unit price comparisons
are not normalized.

The extraction queue and SSE replay remain in memory: process restarts lose
pending jobs and event history, while committed POs and chat remain available.
A forced crash during file copy/commit may leave an orphan source file; file and
DB storage do not form one atomic transaction. There is no retention/deletion
policy for persistent documents or conversations yet. Chat timeouts and tool-call
limits are configurable. Real model tool-calling quality depends on the selected
installed model.

### Database and MCP integration tests

Create a separate disposable PostgreSQL database and set `TEST_DATABASE_URL`.
Apply the schema to that test database explicitly, then run:

```bash
DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head
cd ..
backend/.venv/bin/python -m unittest -v backend.tests.test_po_validation backend.tests.test_po_integration
```

These tests exercise PostgreSQL, real MCP over local HTTP, and an Ollama HTTP
fixture. They do not use a downloaded model. They clean up their own records and
skip when `TEST_DATABASE_URL` is absent. They require local socket access.
`TEST_DATABASE_URL` must be exported into the environment; these test commands
do not automatically load `.env`. Use `alembic check` to detect model/schema drift.
