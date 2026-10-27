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
- [x] Create backend/
- [x] Create frontend/
- [x] Create Python virtual environment .venv
- [x] Activate virtual environment
- [x] Install required Python libraries
- [x] Save installed libraries into requirements.txt
- [x] Create .gitignore
- [x] Create README.md
- [x] Create checklist.md

## Azure Blob Storage
- [x] Create Azure Storage Account
- [x] Create Blob container documents
- [x] Get Storage connection string
- [x] Add Storage connection string to backend/.env
- [x] Add Blob container name to backend/.env
- [x] Test Blob Storage connection from Python
- [x] Upload valid test document to Blob Storage
- [x] Confirm file exists in Azure Portal

## Azure Cosmos DB
- [x] Create Azure Cosmos DB account
- [x] Create database ragdb
- [x] Create container documents
- [x] Set partition key /document_id
- [x] Configure autoscale
- [x] Set Maximum RU/s = 1000
- [x] Get Cosmos DB endpoint
- [x] Get Cosmos DB primary key
- [x] Add Cosmos endpoint to backend/.env
- [x] Add Cosmos key to backend/.env
- [x] Add database name to backend/.env
- [x] Add container name to backend/.env
- [x] Test Cosmos DB connection from Python
- [x] Confirm records appear in Data Explorer

## Backend
- [x] Create backend/main.py
- [x] Create backend/validator.py
- [x] Create backend/blob_service.py
- [x] Create backend/cosmos_service.py
- [x] Create backend/test_connections.py
- [x] Confirm Blob Storage connection
- [x] Confirm Cosmos DB connection
- [x] Create simple FastAPI backend
- [x] Create POST /upload
- [x] Add CORS
- [x] Run backend locally

## Validation Rules
- [x] Accept PDF
- [x] Accept DOCX
- [x] Maximum file size = 10 MB
- [x] Reject empty file
- [x] Reject unsupported file
- [x] Reject corrupted PDF
- [x] Reject corrupted DOCX
- [x] Reject password-protected PDF
- [x] Accept scanned PDF

## Cosmos DB Result
Store:
- [x] document_id
- [x] file_name
- [x] status
- [x] error_reason
- [x] blob_url
- [x] created_at
- [x] validated_at

Statuses:
- [x] Valid
- [x] Invalid

Valid document behavior:
- [x] status = Valid
- [x] error_reason = null
- [x] blob_url saved

Invalid document behavior:
- [x] status = Invalid
- [x] error_reason saved
- [x] Invalid file is not uploaded to Blob Storage

## Frontend — HTML / CSS / JavaScript
- [x] Create frontend/index.html
- [x] Create frontend/style.css
- [x] Create frontend/app.js
- [x] Add file chooser
- [x] Add Upload button
- [x] Send file to FastAPI /upload
- [x] Show success message
- [x] Show validation error
- [x] Run frontend locally
- [x] Test using browser DevTools / Network tab

## End-to-End Testing

Valid file:
- [x] Upload valid document from website
- [x] FastAPI receives document
- [x] Validation succeeds
- [x] Valid document uploads to Blob Storage
- [x] Cosmos DB saves Valid
- [x] blob_url saved
- [x] Website shows success

Invalid file:
- [x] Upload invalid image/file
- [x] Validation rejects file
- [x] Cosmos DB saves Invalid
- [x] error_reason saved
- [x] Website shows validation error

## GitHub
- [x] Confirm backend/.env is ignored
- [x] Initialize Git repository
- [x] Configure Git user name/email
- [x] Commit Day 1
- [x] Create GitHub repository
- [x] Connect local repository to GitHub
- [x] Push branch main
- [x] Day 1 work saved in GitHub

## Verification Notes

Verified on 2026-10-18 from the current source files, installed libraries, local server responses, read-only Azure checks, and Git state. Required environment values are present; no secret values are included here. Blob Storage contains uploaded files. Cosmos DB contains Valid records with blob URLs and Invalid records with error reasons, using the listed fields. The partition key is /document_id and autoscale maximum throughput is 1000 RU/s.

Checked implementation items reflect the behavior present in the source; they do not claim that every validation case was retested. Scanned PDFs are accepted without text extraction. The DOCX validator checks ZIP structure and the presence of word/document.xml; it does not perform full document integrity validation. Successful Cosmos key authentication was verified, but primary versus secondary key identity was not independently established.

Manual testing and primary-key retrieval items are marked complete based on the project owner's confirmation. Azure Portal, Data Explorer, DevTools, and website interactions were not independently observed during this checklist update. The frontend implements both messages, and existing Azure records support the backend storage results.

# DAY 1 STATUS

✅ COMPLETE

All Day 1 checklist items are complete, including manual testing confirmed by the project owner.

Current working flow:

HTML/CSS/JavaScript Website
→ FastAPI
→ Document Validation
→ Valid document → Azure Blob Storage
→ Validation result → Cosmos DB
→ Success/Error returned to user
→ Project pushed to GitHub

---

# DAY 2 — Extraction / OCR

Azure service: Azure Document Intelligence.

## Completed

- [x] Azure Document Intelligence resource created
- [x] Endpoint and key added to backend/.env
- [x] azure-ai-documentintelligence installed
- [x] requirements.txt updated
- [x] Document Intelligence connection test added
- [x] test_extract.py created and tested
- [x] Valid PDFs extracted successfully
- [x] Text extraction verified
- [x] Page count verified
- [x] Paragraph count verified
- [x] Table count verified
- [x] Extraction JSON structure reviewed
- [x] Extracted JSON saved automatically to extracted-documents Blob container
- [x] document_id stored
- [x] file_name stored
- [x] original_page_count stored
- [x] extracted_page_count stored
- [x] extracted_json_url stored
- [x] status updated in Cosmos DB
- [x] status becomes Extracted on success
- [x] status becomes Failed when extraction fails
- [x] error_reason saved for extraction failure
- [x] invalid document stops before entering the pipeline
- [x] valid upload automatically triggers extraction
- [x] end-to-end Day 2 flow tested successfully from the website

## Additional Completed Items

- [x] Create backend/extraction_service.py
- [x] Read document from Blob Storage
- [x] Extract scanned PDF using OCR
- [x] Handle DOCX with the simplest supported approach
- [x] Keep page number
- [x] Keep headings where available
- [x] Keep bounding region/polygon if available

Definition of Done:

```text
Valid website upload
→ Validation
→ Blob Storage
→ Document Intelligence
→ structured extracted JSON saved to extracted-documents
→ Cosmos DB metadata and status updated to Extracted
```

- [x] Verify extraction flow end to end from the website
- [x] Verify scanned PDF OCR flow

---

# DAY 3 — Chunking

Implemented: `backend/chunking_service.py`.

Earlier completed baseline (history; superseded by the strategy below):

- Chunk size = 400 tokens
- Overlap = 80 tokens

The previous token-first approach was improved to structure-aware / semantic chunking. Token size is now a secondary safety limit, not the primary grouping strategy.

Existing chunk metadata retained:

```text
chunk_id
document_id
content
file_name
page_number
chunk_index
```

Preserve meaningful heading/section/location context where available.

## Previous Completed Work

- [x] Understood chunking purpose and strategies
- [x] Selected token-based chunking
- [x] Text chunk size set to 400 tokens
- [x] Text overlap set to 80 tokens
- [x] Preserved useful metadata for citation/highlighting
- [x] Preserved spans, bounding regions, polygons and section metadata
- [x] Separated normal text chunks from table chunks
- [x] Preserved table headers, rows, columns and cell relationships
- [x] Linked text chunks with related table chunks
- [x] Verified all non-table text is preserved
- [x] Verified tables are not duplicated inside text chunks
- [x] Verified all source tables become table chunks
- [x] Verified unique chunk IDs and sequential indexes
- [x] Verified chunk token counts and overlap
- [x] Verified page/source metadata
- [x] Verified original extracted JSON remains unchanged
- [x] Created chunked-documents Blob container
- [x] Saved chunked JSON to Blob Storage
- [x] Updated Cosmos DB with chunking metadata/status
- [x] Connected extraction -> chunking automatically
- [x] Tested upload -> extraction -> chunking end to end
- [x] Verified chunked JSON exists in Blob
- [x] Verified Cosmos status and chunk metadata

## Current Completed Strategy

- [x] Improved the previous token-first approach to structure-aware / semantic chunking
- [x] Use document headings and sections to identify context and group related content
- [x] Keep content from the same section together when appropriate
- [x] Divide large sections when necessary instead of blindly splitting the whole document by token count
- [x] Use token size as a secondary constraint/safety limit
- [x] Add contextual enrichment for better embedding and retrieval
- [x] Preserve meaningful document structure/context where available
- [x] Handle tables with their relevant section/context
- [x] Handle repeated headers, footers, page numbers, and unnecessary page noise so they do not pollute chunk content
- [x] Preserve existing useful chunk metadata and JSON structure
- [x] Support saving chunking JSON to the Azure Blob Storage container: `chunking-documents`
- [x] Keep the implementation generic for different user-uploaded PDF/DOCX documents, without hardcoding test documents
- [x] Test the updated approach with documents containing different layouts and structures

The current strategy and testing are marked complete based on the project owner's confirmation. The earlier `chunked-documents` checkpoint work above remains as history; the current chunking JSON save capability uses `chunking-documents`.

Definition of Done:

```text
Document structure
→ identify section/context
→ group related content
→ contextual enrichment
→ split when necessary
→ token limit as secondary constraint
→ final chunk
```

Day 3 — Chunking: COMPLETE

---

# DAY 4 — Embeddings + Azure AI Search

The embedding/indexing pipeline now receives the improved, contextually enriched chunks produced by the updated chunking stage.

## Understanding / Design

- [x] Understand embeddings
- [x] Understand vector dimensions
- [x] Understand similarity / cosine similarity concept
- [x] Understand Top-K
- [x] Understand vector vs keyword vs hybrid search
- [x] Understand Azure AI Search index
- [x] Decide what gets embedded: chunk content and user questions
- [x] Decide what stays metadata: document/source, page, section, and table relationships
- [x] Decide text chunks and table chunks use the same index
- [x] Decide vectors are stored in Azure AI Search, not Blob

## Azure Setup

- [x] Create Microsoft Foundry resource/project
- [x] Deploy text-embedding-3-small
- [x] Confirm embedding dimension = 1536
- [x] Create Azure AI Search resource
- [x] Add Foundry configuration to .env
- [x] Add Azure AI Search configuration to .env
- [x] Test Azure service connections successfully

Connection test result achieved (project owner confirmed):

```text
Blob Storage: PASS
Cosmos DB: PASS
Document Intelligence: PASS
Foundry Embedding: PASS
Embedding dimension: 1536
Azure AI Search service connection: PASS
Overall: PASS
```

## Backend Embedding

- [x] Create backend/embedding_service.py
- [x] Generate embedding for chunk content
- [x] Generate embedding for user questions
- [x] Validate returned vectors
- [x] Use text-embedding-3-small
- [x] Use 1536-dimensional vectors
- [x] Keep embeddings in memory before indexing
- [x] Do not save embedded output to Blob

## Azure AI Search

- [x] Create backend/search_service.py for index setup, chunk indexing, and vector retrieval
- [x] Create rag-index automatically from backend Python
- [x] Configure content_vector with 1536 dimensions
- [x] Configure vector search / HNSW with cosine similarity
- [x] Store chunk content + vector + metadata
- [x] Index text chunks
- [x] Support table chunks in the same index
- [x] Preserve document ID, filename, pages, chunk index/type, sections, spans, bounding regions, and table relationships where available
- [x] Verify rag-index in Azure portal
- [x] Verify a real indexed chunk in Search Explorer

## Pipeline Integration

- [x] Connect extraction → chunking → embedding → Azure AI Search indexing → Ready
- [x] Cosmos status flow: Valid → Extracting → Chunking → Embedding → Indexing → Ready
- [x] Failure flow: any failed processing stage → Failed → error_reason

## Retrieval

- [x] Add question retrieval endpoint: POST /ask
- [x] Embed user question using the same embedding model
- [x] Filter by document_id using pre-filtering
- [x] Perform vector search
- [x] Use Top-K retrieval
- [x] Test Top-K successfully
- [x] Confirm retrieved chunks are ranked by relevance
- [x] Confirm correct sections rank first in real tests

Current testing setting:

- Top-K = 10 for testing
- Return up to 10 chunks
- If document has fewer chunks, return only available chunks

## Frontend

- [x] Upload flow works
- [x] Full processing flow works
- [x] Frontend progress/status works
- [x] Question input works
- [x] Retrieved passages displayed
- [x] Multiple returned chunks can be displayed
- [x] Add document listing and deletion with related Search/Blob/Cosmos cleanup

## Storage Cleanup

- [x] Remove runtime dependency on extracted-documents Blob container
- [x] Remove runtime dependency on chunked-documents Blob container
- [x] Keep original document in Blob
- [x] Keep extracted_data, chunked_data, embedded_data in backend memory between stages
- [x] Keep permanent searchable chunks/vectors in Azure AI Search

## Implemented Index Fields

The index uses `chunk_id` as its key and `content_vector` for vectors (rather than the earlier planned `id` / `contentVector` names).

```text
chunk_id
content
content_vector (1536 dimensions, HNSW/cosine)
document_id (filterable)
file_name
page_number / page_numbers
chunk_index / chunk_type
section_heading
source_blob_url
spans / bounding_regions
related_table_ids / related_text_chunk_ids
cells and table metadata where available
```

## Verification Notes

This checklist update inspected the current implementation. Understanding/design, Azure setup, Portal/Search Explorer checks, and real retrieval/website results are recorded from the project owner's completion report; those manual and live Azure checks were not repeated during this update.

During this update, frontend regression checks passed again. A fresh backend test run was stopped after it stalled; it did not complete, so the earlier backend pass below is historical evidence.

Existing verification record dated 2026-10-25:

- 54 offline backend tests passed for Search indexing, document-filtered Top-K retrieval, upload orchestration, in-memory processing, and document cleanup.
- Frontend regression checks passed for streamed upload, progress stages, retrieval counts/ranking, document selection/deletion, and theme persistence.
- Azure connection checks passed for Blob Storage, Cosmos DB, Document Intelligence, Foundry embeddings (1536 dimensions), and the existing `rag-index`.
- Successful real-question retrieval quality and website testing were confirmed by the project owner in the prior project conversation; those manual quality checks were not repeated during this checklist update.

The Day 2/3 Blob checkpoint items above describe the earlier completed implementation. Day 4 supersedes those intermediate saves; the current processing path does not read, save, or require extracted/chunked Blob JSON. This does not claim the old Azure containers or historical blobs were deleted. A legacy nullable `extracted_json_url` schema field remains for compatibility, but new indexed chunks omit it.

# DAY 4 STATUS

✅ COMPLETE

Current working flow:

```text
Website upload + progress
→ Validation → Original document in Blob
→ Document Intelligence extraction in memory
→ Text/table chunks in memory
→ Foundry embeddings (1536 dimensions)
→ rag-index chunks + vectors + metadata
→ Cosmos status Ready
→ Website question → question embedding
→ document_id-filtered Top-K vector retrieval
→ Ranked chunks shown in the website
```

Definition of Done:

- [x] A test question returns relevant chunks from the selected document

`POST /ask` currently returns retrieved chunks. LLM-generated grounded answers remain Day 5 work.

Day 4 Status: COMPLETE

Next:
Day 5 — LLM Answer Generation

- [ ] Use retrieved chunks as context
- [ ] Send question + retrieved context to LLM
- [ ] Generate final grounded answer
- [ ] Add citations/source references

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
    