# Azure RAG document workspace

A FastAPI backend and plain JavaScript frontend using Azure Blob Storage,
Cosmos DB, Document Intelligence, Azure AI Search, and Azure OpenAI.

## Phase 1 document management and notifications

- Upload multiple files with per-file validation and streamed pipeline progress.
  Limits are 20 active documents, 10 MB per file, and two PDF pages. Empty,
  corrupt, encrypted, unsupported, and duplicate files are rejected. DOCX files
  must be converted to PDF because their page count cannot be verified reliably.
- Document metadata and capacity stay synchronized after uploads, failures,
  and deletion. Processing documents refresh automatically.
- Select All, View Selected, and Delete Selected operate on explicit document
  IDs. The PDF viewer switches among selected files; DOCX preview is unavailable.
  Deletion requires confirmation and reports individual failures for retry.
- Cleanup removes document-scoped Search chunks, original Blobs and recorded
  artifacts, then Cosmos metadata. Shared Blob references are preserved.
  Processing documents cannot be deleted.
- Failed uploads trigger compensating cleanup. Pending cleanup uses Cosmos
  markers and a local journal, with a background retry every 30 seconds while
  the backend runs. Failed uploads are excluded from the active list and capacity.
- Toast notifications display for five seconds, with manual dismissal and a
  queue of up to three visible messages. The notification bell shows an unread
  count and supports reading and clearing history.
- Notifications include filenames or the submitted question, operation, outcome,
  reason, and timestamp. Up to 100 notifications persist in browser local storage;
  answers and retrieved document contents are excluded. Filenames and questions
  remain on that browser until history is cleared.

The existing question flow retrieves up to four chunks from all Ready documents
or one selected Ready document and sends their context to Azure OpenAI for an
answer. Question and answer history stays in memory for the current page session.
Retrieved passages are available in a debugging panel. This documents existing
code; it does not certify answer quality or completion of citation/highlight work.

## Local setup

Use Python 3.12 and install the existing pinned dependencies:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Configure the environment variables read by the backend services in a local,
ignored `backend/.env`. Never commit keys, connection strings, private endpoints,
uploaded documents, cleanup journals, database exports, or service logs.

Start the backend and frontend in separate terminals:

```powershell
.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000
.venv\Scripts\python.exe -m http.server 5500 --bind 127.0.0.1
```

Open `http://127.0.0.1:5500/frontend/`. These commands start the local development
workspace; the application is not documented here as a public deployment.

## Validation

The unit tests mock Azure services. Run them without starting the backend:

```powershell
.venv\Scripts\python.exe -m unittest backend.test_deletion backend.test_uploads backend.test_llm_service backend.test_chunking_service
node --check frontend/app.js
node --check frontend/test_ui.cjs
node frontend/test_ui.cjs
.venv\Scripts\python.exe -m pip check
```

Chunking tests require the public `cl100k_base` tokenizer cache. Set
`TIKTOKEN_CACHE_DIR` if it is already cached in a non-default location. The cached
sample comparison is optional and skips when local sample extractions are absent.
`backend/test_connections.py` contacts live Azure services and is intentionally
separate from offline unit validation. Local evaluation scripts, experiments,
documents, and generated results are outside this Phase 1 commit.
