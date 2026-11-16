import os
import uuid
from urllib.parse import unquote, urlsplit

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
from dotenv import load_dotenv


load_dotenv("backend/.env")

CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.getenv("AZURE_STORAGE_CONTAINER")


def upload_to_blob(file_name: str, file_bytes: bytes, *, document_id: str = None):
    blob_service = BlobServiceClient.from_connection_string(
        CONNECTION_STRING
    )

    container = blob_service.get_container_client(
        CONTAINER_NAME
    )

    file_name = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    blob_name = f"{document_id or uuid.uuid4()}_{file_name}"
    blob = container.get_blob_client(blob_name)

    blob.upload_blob(
        file_bytes,
        overwrite=False
    )

    return blob.url


def _document_blob_targets(record, account_url, *, include_artifacts=False, strict=True):
    """Resolve exact metadata references, never a filename search or container delete."""
    account = urlsplit(account_url)
    prefix = account.path.rstrip("/") + "/"
    targets = {}
    name = (record.get("file_name") or "").replace("\\", "/").rsplit("/", 1)[-1]
    references = [("original", record.get("blob_url"), CONTAINER_NAME)]
    if include_artifacts:
        references.append(("extracted_json", record.get("extracted_json_url"), None))
        references.extend((f"artifact_{index}", url, None)
                          for index, url in enumerate(record.get("artifact_urls") or []))
    for label, url, container_name in references:
        if url:
            try:
                parsed = urlsplit(url)
                if parsed.netloc != account.netloc or not parsed.path.startswith(prefix):
                    raise ValueError("Document Blob URL is outside the configured storage account")
                parts = unquote(parsed.path[len(prefix):]).split("/", 1)
                if (len(parts) != 2 or not all(parts)
                        or (container_name and parts[0] != container_name)):
                    raise ValueError("Document Blob URL is outside its expected container")
                targets[label] = tuple(parts)
            except (ValueError, TypeError):
                if strict:
                    raise
                # Invalid references cannot match a valid target. Preserve all
                # other valid references from this record in the shared set.
        elif label == "original" and name:
            targets[label] = (container_name, f"{record['document_id']}_{name}")
    return targets


def original_document_exists(record: dict) -> bool:
    """Check only the original in the configured account and document container."""
    with BlobServiceClient.from_connection_string(CONNECTION_STRING) as client:
        container, name = _document_blob_targets(record, client.url)["original"]
        return client.get_blob_client(container=container, blob=name).exists()


def read_original_document(record: dict):
    """Use backend credentials; never return a storage credential or SAS URL."""
    with BlobServiceClient.from_connection_string(CONNECTION_STRING) as client:
        container, name = _document_blob_targets(record, client.url)["original"]
        content = client.get_blob_client(container=container, blob=name).download_blob().readall()
        return content, record.get("file_name") or name.rsplit("/", 1)[-1]


def delete_document_blobs(record: dict, other_records: list) -> dict:
    """Remove originals and recorded artifacts, preserving shared references."""
    with BlobServiceClient.from_connection_string(CONNECTION_STRING) as client:
        targets = _document_blob_targets(record, client.url, include_artifacts=True)
        if other_records is None:
            # New uploads have ID-owned Blob names. They can be compensated
            # safely even when Cosmos cannot supply the sharing inventory.
            prefixes = tuple(record["document_id"] + suffix for suffix in ("_", "/", "."))
            if any(not name.startswith(prefixes) for _, name in targets.values()):
                raise RuntimeError("Shared Blob ownership could not be verified; retry cleanup")
        shared = set()
        for other in other_records or []:
            shared.update(_document_blob_targets(other, client.url, include_artifacts=True, strict=False).values())
        cleanup = {}
        failures = []
        for label, target in targets.items():
            if target in shared:
                cleanup[label] = "shared_preserved"
                continue
            try:
                client.get_blob_client(container=target[0], blob=target[1]).delete_blob(delete_snapshots="include")
                cleanup[label] = "deleted"
            except ResourceNotFoundError:
                cleanup[label] = "already_missing"
            except Exception:
                failures.append(label)
        if failures:
            raise RuntimeError("Some document Blobs could not be deleted; retry cleanup")
        return cleanup
