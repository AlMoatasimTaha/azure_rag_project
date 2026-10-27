import os
from urllib.parse import unquote, urlsplit

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
from dotenv import load_dotenv


load_dotenv("backend/.env")

CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.getenv("AZURE_STORAGE_CONTAINER")


def upload_to_blob(file_name: str, file_bytes: bytes):
    blob_service = BlobServiceClient.from_connection_string(
        CONNECTION_STRING
    )

    container = blob_service.get_container_client(
        CONTAINER_NAME
    )

    blob = container.get_blob_client(file_name)

    blob.upload_blob(
        file_bytes,
        overwrite=True
    )

    return blob.url


def _document_blob_targets(record, account_url):
    """Resolve only the original document in the configured account/container."""
    account = urlsplit(account_url)
    prefix = account.path.rstrip("/") + "/"
    targets = {}
    for label, field, container_name, fallback in (
        ("original", "blob_url", CONTAINER_NAME, record["file_name"]),
    ):
        url = record.get(field)
        if url:
            parsed = urlsplit(url)
            if parsed.netloc != account.netloc or not parsed.path.startswith(prefix):
                raise ValueError("Document Blob URL is outside the configured storage account")
            parts = unquote(parsed.path[len(prefix):]).split("/", 1)
            if len(parts) != 2 or parts[0] != container_name or not parts[1]:
                raise ValueError("Document Blob URL is outside its expected container")
            targets[label] = tuple(parts)
        else:
            targets[label] = (container_name, fallback)
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
        return content, name.rsplit("/", 1)[-1]


def delete_document_blobs(record: dict, other_records: list) -> dict:
    """Remove only the original Blob, preserving names shared by other records."""
    with BlobServiceClient.from_connection_string(CONNECTION_STRING) as client:
        targets = _document_blob_targets(record, client.url)
        shared = set()
        for other in other_records:
            shared.update(_document_blob_targets(other, client.url).values())
        cleanup = {}
        for label, target in targets.items():
            if target in shared:
                cleanup[label] = "shared_preserved"
                continue
            try:
                client.get_blob_client(container=target[0], blob=target[1]).delete_blob(delete_snapshots="include")
                cleanup[label] = "deleted"
            except ResourceNotFoundError:
                cleanup[label] = "already_missing"
        return cleanup
