import json
import os
from io import BytesIO
from pathlib import PurePosixPath

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv
from pypdf import PdfReader


load_dotenv("backend/.env")


def extract_document(
    file_name: str, file_bytes: bytes, *, document_id: str, blob_url: str
):
    client = DocumentIntelligenceClient(
        endpoint=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"),
        credential=AzureKeyCredential(
            os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        ),
    )

    content_type = (
        "application/pdf"
        if PurePosixPath(file_name).suffix.lower() == ".pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    poller = client.begin_analyze_document(
        "prebuilt-layout",
        body=BytesIO(file_bytes),
        content_type=content_type,
    )
    result = poller.result()
    extraction_result = result.as_dict()
    original_page_count = None
    if PurePosixPath(file_name).suffix.lower() == ".pdf":
        original_page_count = len(PdfReader(BytesIO(file_bytes)).pages)
    # DOCX does not have a reliable page count without rendering it.
    extracted_page_count = len(extraction_result.get("pages") or [])

    blob_service = BlobServiceClient.from_connection_string(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    )
    container = blob_service.get_container_client("extracted-documents")
    try:
        container.create_container()
    except ResourceExistsError:
        pass

    json_name = str(PurePosixPath(file_name).with_suffix(".json"))
    blob = container.get_blob_client(json_name)
    metadata = {
        "document_id": document_id,
        "file_name": file_name,
        "blob_url": blob_url,
        "extracted_json_url": blob.url,
        "status": "Extracted",
        "original_page_count": original_page_count,
        "extracted_page_count": extracted_page_count,
        "error_reason": None,
    }
    extracted_json = json.dumps(
        {**metadata, "extraction_result": extraction_result},
        ensure_ascii=False,
        indent=2,
    )
    blob.upload_blob(
        extracted_json.encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )

    return metadata
