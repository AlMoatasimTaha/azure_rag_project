import os
from io import BytesIO
from pathlib import PurePosixPath

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv
from pypdf import PdfReader


load_dotenv("backend/.env")


def extract_document(
    file_name: str, file_bytes: bytes, *, document_id: str, blob_url: str,
    include_extraction_result: bool = False,
):
    """Extract in memory; optionally include the full result for downstream chunking.

    Never reads/writes Blob or creates containers. The original document URL
    is retained as source metadata only.
    """
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

    metadata = {
        "document_id": document_id,
        "file_name": file_name,
        "blob_url": blob_url,
        "status": "Extracted",
        "original_page_count": original_page_count,
        "extracted_page_count": extracted_page_count,
        "error_reason": None,
    }
    extracted_document = {**metadata, "extraction_result": extraction_result}
    return extracted_document if include_extraction_result else {
        key: value for key, value in extracted_document.items() if key != "extraction_result"
    }
