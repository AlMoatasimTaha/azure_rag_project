from datetime import datetime, timezone
import uuid

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

from backend.validator import validate_document
from backend.blob_service import upload_to_blob
from backend.cosmos_service import save_document_result
from backend.extraction_service import extract_document


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
    file: UploadFile = File(...)
):
    document_id = str(uuid.uuid4())

    created_at = datetime.now(
        timezone.utc
    ).isoformat()

    file_bytes = await file.read()

    is_valid, error_reason = validate_document(
        file.filename,
        file_bytes
    )

    if not is_valid:
        return {
            "document_id": document_id,
            "status": "Invalid",
            "message": error_reason
        }

    blob_url = upload_to_blob(
        file.filename,
        file_bytes
    )

    record = {
        "id": document_id,
        "document_id": document_id,
        "file_name": file.filename,
        "status": "Valid",
        "error_reason": None,
        "blob_url": blob_url,
        "created_at": created_at,
        "validated_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    save_document_result(record)

    try:
        extraction_metadata = extract_document(
            file.filename,
            file_bytes,
            document_id=document_id,
            blob_url=blob_url,
        )
    except Exception:
        record.update({
            "status": "Failed",
            "error_reason": "Document extraction or saving the extracted JSON failed.",
        })
        save_document_result(record)
        return {
            "document_id": document_id,
            "status": "Failed",
            "message": record["error_reason"],
            "blob_url": blob_url,
        }

    record.update(extraction_metadata)
    record["extracted_at"] = datetime.now(timezone.utc).isoformat()
    save_document_result(record)

    return {
        **extraction_metadata,
        "message": "File validated, uploaded, and extracted successfully.",
    }
