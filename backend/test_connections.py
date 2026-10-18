import os

from azure.storage.blob import BlobServiceClient
from azure.cosmos import CosmosClient
from dotenv import load_dotenv


load_dotenv("backend/.env")


def test_blob_storage():
    try:
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        container_name = os.getenv("AZURE_STORAGE_CONTAINER")

        blob_service_client = BlobServiceClient.from_connection_string(
            connection_string
        )

        container_client = blob_service_client.get_container_client(
            container_name
        )

        properties = container_client.get_container_properties()

        print("✅ Blob Storage connection successful")
        print(f"   Container: {container_name}")

        return True

    except Exception as error:
        print("❌ Blob Storage connection failed")
        print(f"   Error: {error}")

        return False


def test_cosmos_db():
    try:
        endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")
        key = os.getenv("AZURE_COSMOS_KEY")
        database_name = os.getenv("AZURE_COSMOS_DATABASE")
        container_name = os.getenv("AZURE_COSMOS_CONTAINER")

        client = CosmosClient(
            endpoint,
            credential=key,
        )

        database = client.get_database_client(database_name)
        container = database.get_container_client(container_name)

        properties = container.read()

        print("✅ Cosmos DB connection successful")
        print(f"   Database: {database_name}")
        print(f"   Container: {properties['id']}")

        return True

    except Exception as error:
        print("❌ Cosmos DB connection failed")
        print(f"   Error: {error}")

        return False


if __name__ == "__main__":
    print("\nTesting Azure connections...\n")

    blob_ok = test_blob_storage()

    print()

    cosmos_ok = test_cosmos_db()

    print("\n-----------------------------")

    if blob_ok and cosmos_ok:
        print("✅ All Azure connections are working")
    else:
        print("❌ One or more Azure connections failed")