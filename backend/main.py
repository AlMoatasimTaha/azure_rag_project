from datetime import datetime, timezone
import json
import uuid

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from backend.validator import validate_document
from backend.blob_service import upload_to_blob
from backend.cosmos_service import save_document_result
from backend.extraction_service import extract_document
from backend.chunking_service import chunk_document, save_chunked_json


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
    for update in _process_document(file.filename, file_bytes):
        final_result = update
    return final_result


def _process_document(file_name: str, file_bytes: bytes):
    """Run one upload; yield real stage changes and the final result."""
    document_id = str(uuid.uuid4())

    created_at = datetime.now(
        timezone.utc
    ).isoformat()

    is_valid, error_reason = validate_document(
        file_name,
        file_bytes
    )

    if not is_valid:
        yield {
            "document_id": document_id,
            "status": "Invalid",
            "message": error_reason
        }
        return

    blob_url = upload_to_blob(
        file_name,
        file_bytes
    )

    record = {
        "id": document_id,
        "document_id": document_id,
        "file_name": file_name,
        "status": "Extracting",
        "error_reason": None,
        "blob_url": blob_url,
        "created_at": created_at,
        "validated_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    save_document_result(record)
    yield {"document_id": document_id, "status": "Extracting", "message": "Extracting document..."}

    try:
        extracted_document = extract_document(
            file_name,
            file_bytes,
            document_id=document_id,
            blob_url=blob_url,
            include_extraction_result=True,
        )
    except Exception:
        record.update({
            "status": "Failed",
            "error_reason": "Document extraction or saving the extracted JSON failed.",
        })
        save_document_result(record)
        yield {
            "document_id": document_id,
            "status": "Failed",
            "message": record["error_reason"],
            "blob_url": blob_url,
        }
        return

    # Keep the full extraction in memory/Blob, not in the Cosmos metadata record.
    extraction_metadata = {
        key: value for key, value in extracted_document.items()
        if key != "extraction_result"
    }
    record.update(extraction_metadata)
    record["extracted_at"] = datetime.now(timezone.utc).isoformat()
    record["status"] = "Chunking"
    save_document_result(record)
    yield {"document_id": document_id, "status": "Chunking", "message": "Creating and saving chunks..."}

    stage = "Creating text and table chunks"
    try:
        chunked_document = chunk_document(extracted_document)
        stage = "Saving chunked JSON to Blob Storage"
        chunked_json_url = save_chunked_json(chunked_document)
        chunking_metadata = {
            "status": "Chunked",
            "error_reason": None,
            "chunked_json_url": chunked_json_url,
            "text_chunk_count": len(chunked_document["text_chunks"]),
            "table_chunk_count": len(chunked_document["table_chunks"]),
            "chunk_size": chunked_document["chunking"]["text_chunk_size"],
            "chunk_overlap": chunked_document["chunking"]["text_overlap"],
            "chunked_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as error:
        # ValueError messages from chunking explain missing spans/configuration.
        # Avoid returning raw Azure errors, which can include connection details.
        detail = f": {error}" if isinstance(error, ValueError) else ""
        record.update({
            "status": "Failed",
            "error_reason": f"{stage} failed ({type(error).__name__}){detail}.",
        })
        save_document_result(record)
        yield {
            **extraction_metadata,
            "status": "Failed",
            "error_reason": record["error_reason"],
            "message": record["error_reason"],
        }
        return

    record.update(chunking_metadata)
    save_document_result(record)
    yield {
        **extraction_metadata,
        **chunking_metadata,
        "message": "File validated, uploaded, extracted, and chunked successfully.",
    }
