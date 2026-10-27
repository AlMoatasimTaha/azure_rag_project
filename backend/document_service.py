"""Small document list/delete coordinator; no upload or processing changes."""

import logging
from urllib.parse import urlsplit, urlunsplit

from backend.cosmos_service import list_document_records, get_document_record, delete_document_record
from backend.blob_service import delete_document_blobs, original_document_exists, read_original_document
from backend.search_service import delete_document_chunks


PROCESSING_STATUSES = {"Valid", "Extracting", "Chunking", "Embedding", "Indexing"}
logger = logging.getLogger(__name__)


def list_documents():
    documents = []
    for record in list_document_records():
        try:
            exists = original_document_exists(record)
        except (ValueError, KeyError):
            logger.warning("Skipping document: %r - invalid original Blob location", record.get("document_id"))
            continue
        if not exists:
            logger.info("Skipping stale document: %r - original Blob missing", record["document_id"])
            continue
        url = record.get("blob_url")
        # Do not expose any SAS token stored in a historical metadata URL.
        if url:
            parsed = urlsplit(url)
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        documents.append({
            "document_id": record["document_id"], "file_name": record.get("file_name"),
            "status": record.get("status"), "created_at": record.get("created_at"),
            "blob_url": url,
        })
    return sorted(documents, key=lambda item: item["created_at"] or "", reverse=True)


def view_document(document_id):
    record = get_document_record(document_id)
    if record is None:
        return None
    return read_original_document(record)


def delete_document(document_id):
    record = get_document_record(document_id)
    if record and record.get("status") in PROCESSING_STATUSES:
        raise ValueError("This document is still processing. Wait until it finishes before deleting it.")
    # Search is always cleaned, including when a metadata record is already missing.
    # Leave metadata until last so a partial cleanup can be retried from the UI.
    count = delete_document_chunks(document_id)
    cleanup = {"original": "not_locatable"}
    if record:
        others = [item for item in list_document_records() if item["document_id"] != document_id]
        cleanup = delete_document_blobs(record, others)
        delete_document_record(record)
    return {
        "document_id": document_id, "status": "Deleted",
        "message": "Document deleted." if record else "Document already absent; indexed chunk cleanup completed.",
        "deleted_chunk_count": count, "blob_cleanup": cleanup,
        "metadata_cleanup": "deleted" if record else "already_missing",
    }
