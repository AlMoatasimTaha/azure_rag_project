# Azure RAG Project — 1 Week Checklist

## Main Goal

Build a simple real website where a user can:

- Upload PDF/DOCX documents
- Validate documents
- Store valid documents in Azure Blob Storage
- Save document status/errors in Cosmos DB
- Extract text/OCR using Azure Document Intelligence
- Chunk extracted content
- Create embeddings
- Store/search chunks using Azure AI Search
- Ask questions from the website
- Get grounded answers from uploaded documents
- Show document name and page number citations

---

# Main Rule

> EASY AND SIMPLE FIRST.

- Do not add tools unless needed.
- Do not add advanced features before the basic flow works.
- Understand each part before continuing.
- Build one step.
- Test it.
- Then continue.
- Working first.
- Improve later.

Use React, JavaScript, HTML/JSX, and CSS for the frontend; Python and FastAPI for the backend; and the Azure services listed below. Deliver the working MVP within one week.

Do not introduce Docker, Kubernetes, Terraform, Bicep, CI/CD, GitHub Actions, LangChain, agents, complicated frameworks or folder structures, advanced security architecture, or advanced UI frameworks during the MVP.

---

# DAY 1 — Foundation + Upload + Validation + Storage + Status

## Local Project Setup

- [x] Create local rag-project folder
- [x] Create backend folder
- [x] Create frontend folder
- [x] Create Python virtual environment
- [ ] Activate virtual environment
- [x] Install Python libraries
- [x] Save libraries into requirements.txt
- [x] Create .gitignore
- [x] Create README.md
- [x] Create checklist.md

Verified from current files: `.venv/pyvenv.cfg` and installed package directories exist; `requirements.txt` contains pinned libraries. README.md exists but is empty. Environment activation has not been verified.

## Azure Day 1 Resources

### Blob Storage

- [ ] Azure Storage Account
- [ ] Blob container named documents
- [ ] Storage connection string
- [ ] Add Storage values to backend/.env

### Cosmos DB

- [ ] Cosmos DB account
- [ ] Database named ragdb
- [ ] Container named documents
- [ ] Partition key /document_id
- [ ] Cosmos endpoint
- [ ] Cosmos key
- [ ] Add Cosmos values to backend/.env

Environment variables expected:

```dotenv
AZURE_STORAGE_CONNECTION_STRING=
AZURE_STORAGE_CONTAINER=documents

AZURE_COSMOS_ENDPOINT=
AZURE_COSMOS_KEY=
AZURE_COSMOS_DATABASE=ragdb
AZURE_COSMOS_CONTAINER=documents
```

Never put real secret values inside checklist.md. The six variable names exist in backend/.env, but their values are blank. Azure resources and connections are not verified.

## Backend

Create/use:

- [x] Create backend/main.py
- [x] Create backend/validator.py
- [x] Create backend/blob_service.py
- [x] Create backend/cosmos_service.py

These four files exist but are empty; implementation is still pending.

Tasks:

- [ ] Run FastAPI locally
- [ ] Test root endpoint
- [ ] Test /docs
- [ ] Connect to Cosmos DB
- [ ] Create/read test Cosmos record
- [ ] Connect to Blob Storage
- [ ] Upload test file

## Validation

- [ ] Accept PDF
- [ ] Accept DOCX
- [ ] Maximum 10 MB
- [ ] Reject empty file
- [ ] Reject unsupported file
- [ ] Reject corrupted PDF
- [ ] Reject corrupted DOCX
- [ ] Reject encrypted/password-protected PDF
- [ ] Accept scanned PDF

## Upload Flow

Valid flow:

```text
React
→ FastAPI
→ Cosmos DB status = Uploaded
→ Validation
→ Azure Blob Storage
→ Cosmos DB status = Queued
→ React success
```

Invalid flow:

```text
React
→ FastAPI
→ Cosmos DB status = Uploaded
→ Validation fails
→ Cosmos DB status = Failed
→ save error_reason
→ React displays error
```

- [ ] Implement valid upload flow
- [ ] Implement invalid upload flow

Document fields:

```text
document_id
file_name
status
error_reason
blob_url
created_at
updated_at
```

Day 1 statuses:

```text
Uploaded
Queued
Failed
```

## React Frontend

- [ ] Create React app
- [ ] Create simple upload page
- [ ] Select PDF/DOCX
- [ ] Upload button
- [ ] Send FormData to FastAPI
- [ ] Show loading
- [ ] Show success
- [ ] Show error

## Day 1 Definition of Done

Valid file:

```text
React
→ FastAPI
→ Validation
→ Blob Storage
→ Cosmos DB = Queued
```

Invalid file:

```text
React
→ FastAPI
→ Validation fails
→ Cosmos DB = Failed
→ error_reason saved
```

- [ ] Verify valid file flow
- [ ] Verify invalid file flow
- [ ] Test PDF
- [ ] Test DOCX
- [ ] Test scanned PDF
- [ ] Push Day 1 to GitHub

---

# DAY 2 — Extraction / OCR

Create later: `backend/extraction_service.py`.

Azure service: Azure Document Intelligence.

- [ ] Create Document Intelligence Azure resource
- [ ] Add endpoint/key to backend/.env
- [ ] Create backend/extraction_service.py
- [ ] Read document from Blob Storage
- [ ] Extract normal PDF
- [ ] Extract scanned PDF using OCR
- [ ] Handle DOCX with the simplest supported approach
- [ ] Keep text
- [ ] Keep paragraphs
- [ ] Keep page number
- [ ] Keep headings where available
- [ ] Keep tables where useful
- [ ] Keep source file name
- [ ] Keep bounding region/polygon if available
- [ ] Produce simple structured JSON
- [ ] Test output manually

Definition of Done:

```text
Blob Storage
→ Document Intelligence
→ structured extracted content
```

- [ ] Verify extraction/OCR flow

---

# DAY 3 — Chunking

Create later: `backend/chunking_service.py`.

Start simple:

- Chunk size = 256 tokens
- Overlap = 50 tokens

Do not optimize yet.

Each chunk should keep:

```text
chunk_id
document_id
content
file_name
page_number
chunk_index
```

Keep heading/section/location only if already available.

- [ ] Create backend/chunking_service.py
- [ ] Generate chunks
- [ ] Inspect chunks manually
- [ ] Confirm content is understandable
- [ ] Confirm page number is preserved
- [ ] Confirm chunk can be traced back to source document

Definition of Done:

```text
Extracted content
→ chunks + metadata
```

- [ ] Verify chunks and metadata

---

# DAY 4 — Embeddings + Azure AI Search

Create later:

- `backend/embedding_service.py`
- `backend/search_service.py`

Tasks:

- [ ] Create Azure OpenAI embedding deployment
- [ ] Create Azure AI Search
- [ ] Create backend/embedding_service.py
- [ ] Create backend/search_service.py
- [ ] Create simple search index
- [ ] Generate embeddings
- [ ] Upload chunks/vectors
- [ ] Store metadata

Initial index fields:

```text
id
content
contentVector
document_id
file_name
page_number
chunk_index
```

Test:

```text
Question
→ embedding/search
→ relevant chunks
```

Definition of Done:

- [ ] A test question returns the correct relevant chunks

---

# DAY 5 — RAG Question Answering

Create later: `backend/rag_service.py`.

Create: `POST /ask`.

Flow:

```text
React
→ FastAPI
→ Azure AI Search
→ relevant chunks
→ Azure OpenAI
→ grounded answer
→ React
```

- [ ] Create backend/rag_service.py
- [ ] Create POST /ask
- [ ] Connect retrieved context to an Azure OpenAI chat deployment
- [ ] Connect React question form to POST /ask

Prompt behavior:

- [ ] Answer only using retrieved context
- [ ] Do not invent answers
- [ ] If evidence is missing, say there is not enough information in the uploaded documents

Definition of Done:

- [ ] User asks a question from the website and gets a grounded answer

---

# DAY 6 — Citations + Simple UI

Citation MVP:

```text
Source: file.pdf
Page: 3
```

- [ ] Return source filename
- [ ] Return page number
- [ ] Show citation in React
- [ ] Test question where answer does not exist
- [ ] Show insufficient-evidence response
- [ ] Keep frontend clean and simple

Definition of Done:

- [ ] Answer + correct document citation + correct page

---

# DAY 7 — Final Testing + GitHub + Delivery

## Tests

- [ ] Normal PDF
- [ ] DOCX
- [ ] Scanned PDF
- [ ] Password-protected PDF
- [ ] Corrupted PDF
- [ ] Empty file
- [ ] File >10 MB
- [ ] Unsupported file

## RAG Tests

- [ ] Question with answer
- [ ] Question from another page
- [ ] Question not in document
- [ ] Citation file name correct
- [ ] Citation page correct

## Cleanup

- [ ] Remove unused code
- [ ] Remove Streamlit code
- [x] Confirm .env is ignored
- [ ] Confirm secrets are not in GitHub
- [ ] Update requirements.txt
- [ ] Update README.md

The root .gitignore includes backend/.env and frontend/.env. GitHub contents and Git tracking have not been verified.

## GitHub

- [ ] git status
- [ ] add files
- [ ] commit MVP
- [ ] push to GitHub

## Final Demo

1. Open website
2. Upload PDF
3. Validate
4. Process
5. Status becomes Ready
6. Ask question
7. Show grounded answer
8. Show document + page citation
9. Ask unrelated question
10. Show insufficient-evidence response

- [ ] Complete final demo

---

# FINAL MVP FLOW

```text
React Website
→ FastAPI
→ Validation
→ Azure Blob Storage
→ Cosmos DB
→ Azure Document Intelligence
→ Extraction/OCR
→ Chunking
→ Embeddings
→ Azure AI Search
→ Question
→ Retrieve Evidence
→ Azure OpenAI
→ Grounded Answer
→ Document + Page Citation
```

---

# VERSION 2 — AFTER DELIVERY

Do not implement these during MVP unless all required work is already complete:

- [ ] Exact PDF text highlighting
- [ ] Click citation → open exact page
- [ ] Structure-aware chunking
- [ ] Chunking experiments
- [ ] Hybrid search tuning
- [ ] Semantic ranking
- [ ] Advanced authentication
- [ ] Key Vault
- [ ] Monitoring
- [ ] Docker
- [ ] CI/CD
- [ ] Terraform/Bicep
- [ ] Advanced evaluation
- [ ] Advanced UI
    