from datetime import datetime, timezone
import json
import logging
import uuid
from urllib.parse import quote

from azure.core.exceptions import ResourceNotFoundError
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from backend.validator import validate_document
from backend.blob_service import upload_to_blob
from backend.cosmos_service import save_document_result
from backend.extraction_service import extract_document
from backend.chunking_service import chunk_document
from backend.embedding_service import embed_chunks, generate_embedding
from backend.search_service import index_chunks, search_chunks, RETRIEVAL_TOP_K
from backend.document_service import list_documents, delete_document, view_document


logger = logging.getLogger("uvicorn.error.pipeline")

app = FastAPI()

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
    file: UploadFile = File(...),
    progress: bool = False,
):
    file_bytes = await file.read()
    if progress:
        def updates():
            try:
                for update in _process_document(file.filename, file_bytes):
                    yield json.dumps(update) + "\n"
            except Exception:
                # A streaming response has already started, so report unexpected
                # errors as a final progress event instead of a broken stream.
                yield json.dumps({
                    "status": "Failed",
                    "message": "Document processing failed. Check the backend and try again.",
                }) + "\n"

        return StreamingResponse(
            updates(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    def process():
        for update in _process_document(file.filename, file_bytes):
            final_result = update
        return final_result

    return await run_in_threadpool(process)


class AskRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(min_length=1, strict=True)
    document_id: str = Field(min_length=1, strict=True)


@app.post("/ask")
def ask_question(request: AskRequest):
    """Retrieve relevant chunks only; no language model or answer generation."""
    logger.info("Question received")
    stage = "Question embedding"
    try:
        question_vector = generate_embedding(request.question)
        logger.info("Question embedding generated")
        logger.info("Vector dimension: %d", len(question_vector))
        stage = "Vector search"
        # Log the ID with escaped control characters; never log question text/vectors.
        logger.info("Searching document: %r", request.document_id)
        logger.info("Top-K: %d", RETRIEVAL_TOP_K)
        results = search_chunks(question_vector, request.document_id, top_k=RETRIEVAL_TOP_K)
        logger.info("Retrieved %d chunks", len(results))
    except Exception as error:
        logger.error("Retrieval failed; stage=%s; error_type=%s", stage, type(error).__name__)
        raise HTTPException(status_code=502, detail=f"{stage} failed. Check backend configuration and service availability.") from None
    return {
        "question": request.question, "document_id": request.document_id,
        "top_k": RETRIEVAL_TOP_K, "count": len(results), "results": results,
    }


def _process_document(file_name: str, file_bytes: bytes):
    """Pass stage results directly in memory; persist only metadata to Cosmos."""
    document_id = str(uuid.uuid4())
    record = {
        "id": document_id, "document_id": document_id, "file_name": file_name,
        "status": "Valid", "error_reason": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    stage = "Validation"
    operation = "Document validation"

    def transition(status):
        record["status"] = status
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
        transition("Valid")

        stage, operation = "Upload", "Uploading original document"
        record["blob_url"] = upload_to_blob(file_name, file_bytes)
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
        detail = (f": {error}" if operation in (
            "Creating text and table chunks", "Generating embeddings",
            "Indexing chunks in Azure AI Search",
        ) and isinstance(error, (ValueError, TypeError, RuntimeError)) else "")
        record.update({
            "status": "Failed", "failed_stage": stage,
            "error_reason": f"{operation} failed ({type(error).__name__}){detail}.",
        })
        logger.error("Document failed; document_id=%s; stage=%s; error_type=%s",
                     document_id, stage, type(error).__name__)
        try:
            save_document_result(record)
        except Exception as persistence_error:
            # Cosmos itself can be unavailable: return Failed and keep the server alive.
            record["status_persistence_error"] = "Could not save Failed status to Cosmos"
            logger.error("Failed status could not be saved; document_id=%s; error_type=%s",
                         document_id, type(persistence_error).__name__)
        yield {**record, "message": record["error_reason"]}
