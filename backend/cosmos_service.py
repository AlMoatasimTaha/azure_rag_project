import os

from azure.cosmos import CosmosClient
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