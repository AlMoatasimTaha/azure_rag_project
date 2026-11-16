"""Document management and ID-scoped compensating cleanup for failed uploads."""

import logging
from urllib.parse import urlsplit, urlunsplit

from backend.cosmos_service import list_document_records, get_document_record, delete_document_record, save_document_result
from backend.blob_service import delete_document_blobs, original_document_exists, read_original_document
from backend.search_service import delete_document_chunks
from backend.upload_capacity import release_document_slot
from backend.upload_capacity import INACTIVE_STATUSES
from backend.cleanup_journal import save_pending_cleanup, load_pending_cleanups, pending_cleanup_ids, remove_pending_cleanup


PROCESSING_STATUSES = {"Valid", "Uploading", "Extracting", "Extracted", "Chunking", "Chunked", "Embedding", "Indexing"}
logger = logging.getLogger(__name__)


def list_documents():
    documents = []
    pending_ids = pending_cleanup_ids()
    for record in list_document_records():
        if record.get("status") in INACTIVE_STATUSES or record["document_id"] in pending_ids:
            continue
        try:
            exists = original_document_exists(record)
        except (ValueError, KeyError):
            logger.warning("Original unavailable: %r - invalid Blob location", record.get("document_id"))
            exists = False
        url = record.get("blob_url")
        # Do not expose any SAS token stored in a historical metadata URL.
        if url:
            parsed = urlsplit(url)
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        documents.append({
            "document_id": record["document_id"], "file_name": record.get("file_name"),
            "status": "Unavailable" if not exists and record.get("status") == "Ready" else record.get("status"),
            "created_at": record.get("created_at"),
            "blob_url": url,
            "original_available": exists,
            "cleanup_pending": record.get("status") == "Deleting",
        })
    return sorted(documents, key=lambda item: item["created_at"] or "", reverse=True)


def view_document(document_id):
    record = get_document_record(document_id)
    if record is None:
        return None
    return read_original_document(record)


def delete_document(document_id):
    if not isinstance(document_id, str) or not document_id.strip() or document_id == "__upload_capacity__":
        raise ValueError("A document ID is required")
    pending = next((item for item in load_pending_cleanups() if item["document_id"] == document_id), None)
    if pending:
        if not cleanup_failed_upload(pending):
            raise RuntimeError("Compensating cleanup remains pending")
        return {"document_id": document_id, "status": "Deleted", "message": "Compensating cleanup completed."}
    record = get_document_record(document_id)
    if record and record.get("status") in {"Failed", "FAILED_CLEANUP", "PENDING_CLEANUP"}:
        if not cleanup_failed_upload(record):
            raise RuntimeError("Compensating cleanup remains pending")
        return {"document_id": document_id, "status": "Deleted", "message": "Compensating cleanup completed."}
    if record and record.get("status") in PROCESSING_STATUSES:
        raise ValueError("This document is still processing. Wait until it finishes before deleting it.")
    # Search is always cleaned, including when a metadata record is already missing.
    # Leave metadata until last so a partial cleanup can be retried from the UI.
    if record and record.get("status") not in {"FAILED_CLEANUP", "PENDING_CLEANUP"}:
        record = {**record, "status": "Deleting"}
        save_document_result(record)
    return _delete_resources(document_id, record)


def _delete_resources(document_id, record):
    count = delete_document_chunks(document_id)
    cleanup = {"original": "not_locatable"}
    if record:
        others = [item for item in list_document_records() if item["document_id"] != document_id]
        cleanup = delete_document_blobs(record, others)
    release_document_slot(document_id)
    if record:
        delete_document_record(record)
    remove_pending_cleanup(document_id)
    return {
        "document_id": document_id, "status": "Deleted",
        "message": "Document deleted." if record else "Document already absent; indexed chunk cleanup completed.",
        "deleted_chunk_count": count, "blob_cleanup": cleanup,
        "metadata_cleanup": "deleted" if record else "already_missing",
    }


def cleanup_failed_upload(record, *, resources_possible=True):
    """Retain only retry coordinates; failed uploads never occupy active slots.

    This is invoked only for the current failed upload, never as a sweep of
    existing documents. The normal ID-based delete endpoint can retry markers.
    """
    document_id = record["document_id"]
    if not resources_possible:
        try:
            release_document_slot(document_id)
            return True
        except Exception as error:
            logger.error("Failed upload capacity release pending; document_id=%s; error_type=%s",
                         document_id, type(error).__name__)
            # A retry marker is necessary only if reservation release failed.
    marker = {key: record[key] for key in ("id", "document_id", "file_name", "blob_url", "extracted_json_url", "artifact_urls") if key in record}
    marker.update({"id": record.get("id", document_id), "status": "FAILED_CLEANUP",
                   "cleanup_protocol": "compensation-v1",
                   "failed_stage": record.get("failed_stage", "Processing"),
                   "error_reason": record.get("error_reason") or "Document processing failed; cleanup pending."})
    # Write intent before any compensation. Cosmos and the local outbox provide
    # independent retry coordinates, including a successful upload whose Cosmos
    # status update subsequently failed.
    journal_saved = False
    try:
        save_pending_cleanup(marker)
        journal_saved = True
    except Exception as error:
        logger.error("Cleanup journal persistence failed; document_id=%s; error_type=%s", document_id, type(error).__name__)
    marker_saved = False
    try:
        save_document_result(marker)
        marker_saved = True
    except Exception as error:
        # Never discard in-memory coordinates if saving the marker fails. A
        # previously persisted pipeline record remains available for retry.
        logger.error("Failed cleanup marker persistence; document_id=%s; error_type=%s",
                     document_id, type(error).__name__)
    failures = []
    # These compensations are independent: an unavailable Search service must
    # never prevent the original Blob from being deleted (or vice versa).
    def compensate(stage, action):
        try:
            action()
        except Exception as error:
            failures.append(stage)
            logger.error("Failed upload compensation pending; document_id=%s; cleanup_stage=%s; error_type=%s",
                         document_id, stage, type(error).__name__)

    compensate("search", lambda: delete_document_chunks(document_id))
    def cleanup_blobs():
        try:
            others = [item for item in list_document_records() if item["document_id"] != document_id]
        except Exception:
            others = None  # The Blob helper then permits only ID-owned paths.
        delete_document_blobs(marker, others)
    compensate("blobs", cleanup_blobs)
    compensate("capacity", lambda: release_document_slot(document_id))
    if not failures:
        compensate("metadata", lambda: delete_document_record(marker))
    if failures:
        if not journal_saved and not marker_saved:
            logger.critical("Cleanup retry storage unavailable; document_id=%s", document_id)
        return False
    try:
        remove_pending_cleanup(document_id)
    except Exception as error:
        logger.error("Cleanup journal completion pending; document_id=%s; error_type=%s", document_id, type(error).__name__)
        return False
    return True


def retry_pending_cleanups():
    """Retry only compensation intents created by this protocol, never Ready uploads."""
    pending = {item["document_id"]: item for item in load_pending_cleanups()}
    try:
        for item in list_document_records():
            if item.get("cleanup_protocol") == "compensation-v1" and item.get("status") == "FAILED_CLEANUP":
                pending.setdefault(item["document_id"], item)
    except Exception as error:
        logger.warning("Cleanup retry metadata unavailable; error_type=%s", type(error).__name__)
    for item in pending.values():
        try:
            cleanup_failed_upload(item)
        except Exception as error:
            logger.error("Cleanup retry entry failed; document_id=%s; error_type=%s", item["document_id"], type(error).__name__)


def delete_documents(document_ids=None):
    """Snapshot application metadata for Delete All; isolate each document's failure."""
    ids = (list(dict.fromkeys(record["document_id"] for record in list_document_records()))
           if document_ids is None else list(dict.fromkeys(document_ids)))
    results = []
    for document_id in ids:
        try:
            results.append(delete_document(document_id))
        except ValueError:
            results.append({"document_id": document_id, "status": "Failed",
                            "message": "Document is still processing or has invalid stored Blob paths. Please retry."})
        except Exception as error:
            logger.warning("Document cleanup incomplete; document_id=%r; error_type=%s", document_id, type(error).__name__)
            results.append({"document_id": document_id, "status": "Failed",
                            "message": "Cleanup could not finish. Please retry; metadata is retained until cleanup succeeds."})
    return {"results": results, "deleted_count": sum(item["status"] == "Deleted" for item in results),
            "failed_count": sum(item["status"] != "Deleted" for item in results)}
