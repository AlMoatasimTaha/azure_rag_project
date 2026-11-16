from datetime import datetime, timezone
import asyncio
import json
from hashlib import sha256
import logging
from queue import Queue
import uuid
from contextlib import asynccontextmanager, suppress
from urllib.parse import quote

from azure.core.exceptions import ResourceNotFoundError
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from backend.validator import validate_document, MAX_DOCUMENTS, MAX_FILE_SIZE_BYTES
from backend.upload_capacity import get_upload_capacity, reserve_document_slots, CapacityError
from backend.blob_service import upload_to_blob
from backend.cosmos_service import save_document_result
from backend.extraction_service import extract_document
from backend.chunking_service import chunk_document
from backend.embedding_service import embed_chunks, generate_embedding
from backend.search_service import index_chunks, search_chunks, RETRIEVAL_TOP_K
from backend.llm_service import generate_answer, LLMConfigurationError, LLMGenerationError
from backend.document_service import list_documents, delete_document, delete_documents, view_document, cleanup_failed_upload, retry_pending_cleanups


logger = logging.getLogger("uvicorn.error.pipeline")

async def cleanup_retry_worker():
    while True:
        try:
            await run_in_threadpool(retry_pending_cleanups)
        except Exception as error:
            logger.error("Cleanup retry worker failed; error_type=%s", type(error).__name__)
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    worker = asyncio.create_task(cleanup_retry_worker())
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


app = FastAPI(lifespan=lifespan)
# Retain processing tasks if a browser disconnects; do not strand a document at
# an intermediate stage just because its progress stream was closed.
_upload_tasks = set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "message": "RAG backend is running"
    }


@app.get("/documents")
def get_documents(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return list_documents()
    except Exception as error:
        logger.error("Document list failed; error_type=%s", type(error).__name__)
        raise HTTPException(status_code=502, detail="Could not load documents. Check backend storage connectivity.") from None


@app.get("/documents/{document_id}/view")
def get_document_view(document_id: str):
    try:
        original = view_document(document_id)
    except ResourceNotFoundError:
        original = None
    except (ValueError, KeyError):
        raise HTTPException(status_code=404, detail="Original document is unavailable.") from None
    except Exception as error:
        logger.error("Document view failed; document_id=%r; error_type=%s", document_id, type(error).__name__)
        raise HTTPException(status_code=502, detail="Could not open document. Check backend storage connectivity.") from None
    if original is None:
        raise HTTPException(status_code=404, detail="Original document is unavailable.")
    content, file_name = original
    media_type = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(file_name.rpartition(".")[2].lower(), "application/octet-stream")
    return Response(content=content, media_type=media_type, headers={
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(file_name, safe='')}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/documents/capacity")
def document_capacity(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return get_upload_capacity()
    except Exception:
        raise HTTPException(status_code=502, detail="Could not check document capacity. Please retry.") from None


class DeleteDocumentsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_ids: list[str] | None = Field(default=None, min_length=1)
    all_documents: bool = False

    @model_validator(mode="after")
    def validate_scope(self):
        if self.all_documents == (self.document_ids is not None):
            raise ValueError("Provide document_ids or all_documents=true, exclusively")
        if self.document_ids and any(not value.strip() or value == "__upload_capacity__" for value in self.document_ids):
            raise ValueError("Provide nonempty document IDs")
        return self


@app.post("/documents/delete-batch")
def remove_documents(request: DeleteDocumentsRequest):
    try:
        return delete_documents(None if request.all_documents else request.document_ids)
    except Exception as error:
        logger.error("Bulk deletion failed; error_type=%s", type(error).__name__)
        raise HTTPException(status_code=502, detail="Could not list documents for deletion. Please retry.") from None


@app.delete("/documents/{document_id}")
def remove_document(document_id: str):
    try:
        return delete_document(document_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="Document cannot be deleted while processing or its stored Blob paths are invalid.") from None
    except Exception as error:
        logger.error("Document deletion failed; document_id=%r; error_type=%s", document_id, type(error).__name__)
        raise HTTPException(status_code=502, detail="Cleanup could not finish. Retry Delete; metadata is kept until cleanup succeeds.") from None


@app.post("/upload")
async def upload_document(
    files: list[UploadFile] | None = File(None),
    file: UploadFile | None = File(None),
    progress: bool = False,
):
    uploads = list(files or []) + ([file] if file else [])
    if not uploads:
        raise HTTPException(status_code=422, detail="Please select at least one document.")
    if len(uploads) > MAX_DOCUMENTS:
        raise HTTPException(status_code=422, detail="You can upload a maximum of 20 documents.")
    prepared, outcomes = [], []
    for upload in uploads:
        name = (upload.filename or "Untitled").replace("\\", "/").rsplit("/", 1)[-1]
        content = await upload.read(MAX_FILE_SIZE_BYTES + 1)
        await upload.close()
        valid, reason = await run_in_threadpool(validate_document, name, content)
        document_id = str(uuid.uuid4())
        outcomes.append({"document_id": document_id, "file_name": name,
                         "status": "Queued" if valid else "Invalid", "message": reason})
        if valid:
            prepared.append((len(outcomes) - 1, document_id, name, content))
    try:
        # Reserve unique valid files and their fingerprints atomically. Repeated
        # copies are skipped without consuming capacity or entering the pipeline.
        hashes = {item[1]: sha256(item[3]).hexdigest() for item in prepared}
        duplicates = await run_in_threadpool(reserve_document_slots, [item[1] for item in prepared], len(uploads), hashes)
        if isinstance(duplicates, dict):
            for index, document_id, name, _ in prepared:
                if document_id in duplicates:
                    outcomes[index].update({"status": "Invalid", "duplicate": True,
                        "existing_document_id": duplicates[document_id],
                        "message": f"{name} is already uploaded or being processed (or repeated in this selection). This copy was not uploaded."})
            prepared = [item for item in prepared if item[1] not in duplicates]
    except CapacityError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=503, detail="Could not reserve document capacity. Please retry.") from None

    def process(emit=None):
        def notify(index, document_id, name, status, message=None):
            if emit:
                emit({"type": "progress", "file_index": index, "document_id": document_id,
                      "file_name": name, "status": status, "message": message})

        for index, outcome in enumerate(outcomes):
            if outcome["status"] == "Invalid":
                notify(index, outcome["document_id"], outcome["file_name"], "Invalid", outcome["message"])
        for index, document_id, name, content in prepared:
            try:
                notify(index, document_id, name, "Validating")
                for update in _process_document(name, content, document_id=document_id):
                    final_result = update
                    if update["status"] not in {"Ready", "Failed", "Invalid"}:
                        notify(index, document_id, name, update["status"])
                outcomes[index] = {
                    "document_id": document_id, "file_name": name, "status": final_result["status"],
                    "message": "Uploaded successfully" if final_result["status"] == "Ready"
                        else final_result.get("error_reason") or final_result.get("message") or "Document processing failed.",
                    "cleanup_pending": final_result.get("cleanup_pending", False),
                }
            except Exception:
                record = {"id": document_id, "document_id": document_id, "file_name": name,
                          "status": "Failed", "message": "Document processing failed."}
                # Unexpected generator failures still use the persisted resource
                # coordinates, if available, rather than matching a filename.
                from backend.cosmos_service import get_document_record
                try:
                    saved = get_document_record(document_id)
                    record["cleanup_pending"] = not cleanup_failed_upload(saved or record, resources_possible=saved is not None)
                except Exception as error:
                    logger.error("Upload cleanup could not start; document_id=%s; error_type=%s", document_id, type(error).__name__)
                    record["cleanup_pending"] = True
                outcomes[index] = record
            notify(index, document_id, name, outcomes[index]["status"], outcomes[index]["message"])
        return {"results": outcomes}

    if progress:
        events = Queue()

        def worker():
            try:
                return process(events.put)
            finally:
                events.put(None)

        task = asyncio.create_task(run_in_threadpool(worker))
        _upload_tasks.add(task)

        def completed(finished):
            _upload_tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()  # Retrieve failures even if the browser left.

        task.add_done_callback(completed)

        async def stream():
            try:
                while True:
                    event = await run_in_threadpool(events.get)
                    if event is None:
                        summary = await asyncio.shield(task)
                        yield json.dumps({"type": "complete", **summary}) + "\n"
                        break
                    yield json.dumps(event) + "\n"
            except Exception:
                yield json.dumps({"type": "error", "message": "Progress connection failed. Refresh the document list before retrying."}) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return await run_in_threadpool(process)


class AskRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(min_length=1, strict=True)
    document_id: str | None = Field(default=None, min_length=1, strict=True)


@app.post("/ask")
def ask_question(request: AskRequest):
    """Retrieve Top-4 chunks across Ready originals, or one selected Ready original."""
    logger.info("Question received")
    try:
        ready_ids = list(dict.fromkeys(item["document_id"] for item in list_documents()
                                     if item.get("status") == "Ready"))
    except Exception:
        raise HTTPException(status_code=502, detail="Could not load available documents. Please retry.") from None
    if not ready_ids:
        raise HTTPException(status_code=409, detail="No Ready documents are available. Upload documents and wait for processing to finish.")
    if request.document_id is not None and request.document_id not in ready_ids:
        raise HTTPException(status_code=409, detail="The selected document is not available or not Ready. Refresh the document list.")
    stage = "Question embedding"
    try:
        question_vector = generate_embedding(request.question)
        logger.info("Question embedding generated")
        logger.info("Vector dimension: %d", len(question_vector))
        stage = "Vector search"
        # Log the ID with escaped control characters; never log question text/vectors.
        logger.info("Searching scope: %s", request.document_id or f"all {len(ready_ids)} Ready documents")
        logger.info("Top-K: %d", RETRIEVAL_TOP_K)
        if request.document_id is None:
            results = search_chunks(question_vector, document_ids=ready_ids, top_k=RETRIEVAL_TOP_K)
        else:
            results = search_chunks(question_vector, request.document_id, top_k=RETRIEVAL_TOP_K)
        logger.info("Retrieved %d chunks", len(results))
    except Exception as error:
        logger.error("Retrieval failed; stage=%s; error_type=%s", stage, type(error).__name__)
        raise HTTPException(status_code=502, detail=f"{stage} failed. Check backend configuration and service availability.") from None
    try:
        answer = generate_answer(request.question, results)
    except LLMConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    except LLMGenerationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None
    except Exception as error:
        logger.error("Answer generation failed; error_type=%s", type(error).__name__)
        raise HTTPException(status_code=502, detail="Answer generation failed. Check backend configuration and service availability.") from None
    return {
        "question": request.question, "document_id": request.document_id,
        "answer": answer,
        "top_k": RETRIEVAL_TOP_K, "count": len(results), "results": results,
    }


def _pipeline_failure_reason(operation, error):
    """Return useful service diagnostics without SDK bodies, URLs or keys."""
    status = getattr(error, "status_code", None)
    descriptions = {400: "the service rejected the document or request", 401: "service authentication failed",
                    403: "service access was denied", 404: "the configured service resource was not found",
                    408: "the service request timed out", 413: "the service rejected the document size",
                    415: "the document format is unsupported", 429: "the service rate limit was reached"}
    service_error = getattr(error, "error", None)
    code = service_error.get("code") if isinstance(service_error, dict) else getattr(service_error, "code", None)
    safe_codes = {"InvalidContent": "the document content is corrupt, unreadable or unsupported",
                  "UnsupportedContent": "the document format is unsupported",
                  "InvalidContentLength": "the document size is not supported by the service",
                  "ContentSourceNotAccessible": "the service could not access the document source"}
    if isinstance(code, str) and code in safe_codes:
        return f"{operation} failed: {safe_codes[code]} ({code})."
    if isinstance(status, int):
        reason = descriptions.get(status, "the service is temporarily unavailable" if status >= 500 else "the service request failed")
        return f"{operation} failed: {reason} (HTTP {status})."
    if isinstance(error, TimeoutError) or type(error).__name__ in {"ServiceRequestTimeoutError", "ServiceResponseTimeoutError"}:
        return f"{operation} failed: the service request timed out."
    if type(error).__name__ in {"ServiceRequestError", "ServiceResponseError", "APIConnectionError"}:
        return f"{operation} failed: could not connect to the service."
    # These production helpers already expose deliberately sanitized errors.
    detail = (f": {error}" if operation in (
        "Creating text and table chunks", "Generating embeddings", "Indexing chunks in Azure AI Search",
    ) and isinstance(error, (ValueError, TypeError, RuntimeError)) else "")
    return f"{operation} failed ({type(error).__name__}){detail}."


def _process_document(file_name: str, file_bytes: bytes, *, document_id: str = None):
    """Pass stage results directly in memory; persist only metadata to Cosmos."""
    document_id = document_id or str(uuid.uuid4())
    record = {
        "id": document_id, "document_id": document_id, "file_name": file_name,
        "status": "Valid", "error_reason": None,
        "file_sha256": sha256(file_bytes).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    stage = "Validation"
    operation = "Document validation"
    metadata_attempted = False
    upload_attempted = False

    def transition(status):
        nonlocal metadata_attempted
        record["status"] = status
        # An upsert may commit before its acknowledgement is lost.
        metadata_attempted = True
        save_document_result(record)
        return {"document_id": document_id, "status": status,
                "message": f"Document {status}"}

    try:
        is_valid, error_reason = validate_document(file_name, file_bytes)
        if not is_valid:
            yield {"document_id": document_id, "status": "Invalid", "message": error_reason}
            return
        record["validated_at"] = datetime.now(timezone.utc).isoformat()
        operation = "Saving Valid status to Cosmos"
        yield transition("Valid")

        stage, operation = "Upload", "Uploading original document"
        yield {"document_id": document_id, "status": "Uploading"}
        upload_attempted = True
        record["blob_url"] = upload_to_blob(file_name, file_bytes, document_id=document_id)
        logger.info("Document uploaded; document_id=%s", document_id)

        stage, operation = "Extraction", "Saving Extracting status to Cosmos"
        yield transition("Extracting")
        operation = "Document extraction"
        logger.info("Extraction started; document_id=%s", document_id)
        extracted_data = extract_document(
            file_name, file_bytes, document_id=document_id,
            blob_url=record["blob_url"], include_extraction_result=True,
        )
        record.update({key: value for key, value in extracted_data.items()
                       if key not in ("extraction_result", "status", "error_reason")})
        record["extracted_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("Extraction completed in memory; document_id=%s", document_id)

        stage, operation = "Chunking", "Saving Chunking status to Cosmos"
        yield transition("Chunking")
        operation = "Creating text and table chunks"
        logger.info("Chunking started; document_id=%s", document_id)
        chunked_data = chunk_document(extracted_data)
        record.update({
            "text_chunk_count": len(chunked_data["text_chunks"]),
            "table_chunk_count": len(chunked_data["table_chunks"]),
            "chunk_size": chunked_data["chunking"]["text_chunk_size"],
            "chunk_overlap": chunked_data["chunking"]["text_overlap"],
            "chunked_at": datetime.now(timezone.utc).isoformat(),
        })
        total_chunks = record["text_chunk_count"] + record["table_chunk_count"]
        if not total_chunks:
            raise ValueError("No text or table chunks were produced")
        logger.info("Chunking completed in memory; document_id=%s; chunks=%d", document_id, total_chunks)

        stage, operation = "Embedding", "Saving Embedding status to Cosmos"
        yield transition("Embedding")
        operation = "Generating embeddings"
        logger.info("Embedding started; document_id=%s", document_id)
        # Neither this object nor its vectors go to Blob, Cosmos, or responses.
        embedded_data = embed_chunks(chunked_data)
        record["embedded_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("Embedding completed in memory; document_id=%s", document_id)

        stage, operation = "Indexing", "Saving Indexing status to Cosmos"
        yield transition("Indexing")
        operation = "Indexing chunks in Azure AI Search"
        logger.info("Indexing started; document_id=%s", document_id)
        # The production function ensures the index and checks every upload result.
        results = index_chunks(embedded_data)
        if len(results) != total_chunks or any(not result.succeeded for result in results):
            raise RuntimeError("Search did not confirm all chunks; some chunks may already be indexed")
        record["indexed_chunk_count"] = len(results)
        record["indexed_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("Indexed %d chunks; document_id=%s", len(results), document_id)

        operation = "Saving Ready status to Cosmos"
        transition("Ready")
        logger.info("Document Ready; document_id=%s", document_id)
        yield {**record, "message": "File validated, uploaded, extracted, chunked, embedded, and indexed successfully."}
    except Exception as error:
        # Production processing services sanitize their validation/API errors.
        # Storage/SDK failures must never expose raw response data or credentials.
        record.update({
            "status": "Failed", "failed_stage": stage,
            "error_reason": _pipeline_failure_reason(operation, error),
        })
        logger.error("Document failed; document_id=%s; stage=%s; error_type=%s",
                     document_id, stage, type(error).__name__)
        record["cleanup_pending"] = not cleanup_failed_upload(record, resources_possible=metadata_attempted or upload_attempted)
        yield {**record, "message": record["error_reason"]}
