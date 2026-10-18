import os

from azure.storage.blob import BlobServiceClient
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