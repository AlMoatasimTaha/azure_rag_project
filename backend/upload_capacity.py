"""Atomic batch admission using one ETag-protected item in the existing Cosmos container.

Count active metadata and pending admissions; terminal failures do not use slots.
Pending admissions still fail closed after an uncertain server interruption.
"""
from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from hashlib import sha256
from azure.cosmos.exceptions import (
    CosmosResourceNotFoundError, CosmosResourceExistsError, CosmosAccessConditionFailedError,
)

from backend.cosmos_service import container, list_document_records
from backend.validator import MAX_DOCUMENTS
from backend.blob_service import read_original_document
from backend.cleanup_journal import pending_cleanup_ids

CAPACITY_ID = "__upload_capacity__"


class CapacityError(ValueError):
    pass


def _read():
    try:
        return container.read_item(item=CAPACITY_ID, partition_key=CAPACITY_ID)
    except CosmosResourceNotFoundError:
        return None


INACTIVE_STATUSES = {"Invalid", "Failed", "Deleted", "FAILED_CLEANUP", "PENDING_CLEANUP"}


def _counted_ids(state, records=None):
    records = list_document_records() if records is None else records
    # A deletion has already withdrawn the document from Ready retrieval. Keep
    # its retry record, but never re-reserve it between slot release and the
    # final metadata delete when another upload is admitted concurrently.
    excluded = INACTIVE_STATUSES | {"Deleting"}
    inactive = {r["document_id"] for r in records if r.get("status") in excluded} | pending_cleanup_ids()
    active = {r["document_id"] for r in records if r.get("status") not in excluded}
    # Old bookkeeping had no distinct pending-admission semantics. Reconcile it
    # against metadata once; never resurrect IDs of already deleted documents.
    reserved = set((state or {}).get("document_ids", [])) if (state or {}).get("version") == 2 else set()
    return (active | reserved) - inactive


def get_upload_capacity():
    state = _read()
    ids = _counted_ids(state)
    return {"current_count": len(ids), "max_documents": MAX_DOCUMENTS,
            "remaining_slots": max(0, MAX_DOCUMENTS - len(ids))}


def _content_hashes(records, ids, state):
    hashes = {key: value for key, value in (state or {}).get("document_hashes", {}).items() if key in ids}
    for record in records:
        key = record["document_id"]
        if key not in ids:
            continue
        digest = record.get("file_sha256") or hashes.get(key)
        if not digest:
            try:
                content, _ = read_original_document(record)
            except ResourceNotFoundError:
                continue  # No original file remains to duplicate.
            digest = sha256(content).hexdigest()
            # Patch only the fingerprint; an upload may update its status meanwhile.
            container.patch_item(item=record["id"], partition_key=key, patch_operations=[
                {"op": "add", "path": "/file_sha256", "value": digest},
            ])
        hashes[key] = digest
    return hashes


def reserve_document_slots(document_ids, requested_count, document_hashes=None):
    for _ in range(20):
        state = _read()
        records = list_document_records()
        ids = _counted_ids(state, records)
        hashes = (_content_hashes(records, ids, state) if document_ids and document_hashes is not None else
                  {key: value for key, value in (state or {}).get("document_hashes", {}).items() if key in ids})
        owners = {digest: key for key, digest in hashes.items()}
        duplicates, accepted = {}, []
        for key in document_ids:
            digest = (document_hashes or {}).get(key)
            if digest and digest in owners:
                duplicates[key] = owners[digest]
            else:
                accepted.append(key)
                if digest:
                    hashes[key] = digest
                    owners[digest] = key
        remaining = max(0, MAX_DOCUMENTS - len(ids))
        if len(accepted) > remaining:
            raise CapacityError(
                f"The system currently contains {len(ids)} of {MAX_DOCUMENTS} documents. "
                f"You can upload only {remaining} more documents."
            )
        if not accepted:
            return duplicates
        updated = {"id": CAPACITY_ID, "document_id": CAPACITY_ID, "version": 2,
                   "document_ids": sorted(ids | set(accepted)), "document_hashes": hashes}
        try:
            if state is None:
                container.create_item(updated)
            else:
                container.replace_item(item=CAPACITY_ID, body=updated, etag=state["_etag"],
                                       match_condition=MatchConditions.IfNotModified)
            return duplicates
        except (CosmosResourceExistsError, CosmosAccessConditionFailedError):
            continue
    raise RuntimeError("Upload capacity is busy. Please retry.")


def release_document_slot(document_id):
    for _ in range(20):
        state = _read()
        if state is None or document_id not in state.get("document_ids", []):
            return
        updated = {"id": CAPACITY_ID, "document_id": CAPACITY_ID, "version": 2,
                   "document_ids": sorted(_counted_ids(state) - {document_id})}
        updated["document_hashes"] = {key: value for key, value in state.get("document_hashes", {}).items()
                                      if key in updated["document_ids"]}
        try:
            container.replace_item(item=CAPACITY_ID, body=updated, etag=state["_etag"],
                                   match_condition=MatchConditions.IfNotModified)
            return
        except CosmosAccessConditionFailedError:
            continue
    raise RuntimeError("Upload capacity is busy. Please retry deletion.")
