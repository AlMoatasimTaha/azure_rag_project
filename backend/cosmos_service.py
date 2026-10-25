import os

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from dotenv import load_dotenv


load_dotenv("backend/.env")

COSMOS_ENDPOINT = os.getenv("AZURE_COSMOS_ENDPOINT")
COSMOS_KEY = os.getenv("AZURE_COSMOS_KEY")
COSMOS_DATABASE = os.getenv("AZURE_COSMOS_DATABASE")
COSMOS_CONTAINER = os.getenv("AZURE_COSMOS_CONTAINER")


client = CosmosClient(
    COSMOS_ENDPOINT,
    credential=COSMOS_KEY
)

database = client.get_database_client(
    COSMOS_DATABASE
)

container = database.get_container_client(
    COSMOS_CONTAINER
)


def save_document_result(record: dict):
    container.upsert_item(record)


def list_document_records() -> list:
    """Read metadata only; keep internal Cosmos fields out of API responses."""
    return list(container.query_items(
        query="SELECT * FROM c WHERE IS_DEFINED(c.document_id)",
        enable_cross_partition_query=True,
    ))


def get_document_record(document_id: str):
    records = list(container.query_items(
        query="SELECT * FROM c WHERE c.document_id = @document_id",
        parameters=[{"name": "@document_id", "value": document_id}],
        enable_cross_partition_query=True,
    ))
    return records[0] if records else None


def delete_document_record(record: dict):
    # This project's container uses /document_id as its partition key.
    try:
        container.delete_item(item=record["id"], partition_key=record["document_id"])
    except CosmosResourceNotFoundError:
        pass
