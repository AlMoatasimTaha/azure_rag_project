"""Offline document management checks: python -m unittest backend.test_documents."""

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from azure.core.exceptions import ResourceNotFoundError
from azure.cosmos.exceptions import CosmosResourceNotFoundError

with patch("azure.cosmos.CosmosClient"):
    from backend import document_service as service, cosmos_service, main
from backend import blob_service, search_service


class DocumentTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.records = stack.enter_context(patch.object(service, "list_document_records"))
        self.get = stack.enter_context(patch.object(service, "get_document_record"))
        self.blobs = stack.enter_context(patch.object(service, "delete_document_blobs"))
        self.chunks = stack.enter_context(patch.object(service, "delete_document_chunks", return_value=4))
        self.metadata = stack.enter_context(patch.object(service, "delete_document_record"))
        self.record = {"id": "doc-1", "document_id": "doc-1", "file_name": "guide.pdf", "status": "Ready"}
        self.get.return_value = self.record
        self.records.return_value = [self.record]
        self.blobs.return_value = {"original": "deleted"}

    def test_list_is_allowlisted_sorted_and_removes_sas_query(self):
        self.records.return_value = [
            {**self.record, "created_at": "2025-01-01", "_rid": "private", "blob_url": "https://example/guide.pdf?sig=secret#fragment"},
            {**self.record, "document_id": "doc-2", "created_at": "2026-01-01"},
        ]
        data = service.list_documents()
        self.assertEqual(data[0]["document_id"], "doc-2")
        self.assertEqual(set(data[0]), {"document_id", "file_name", "status", "created_at", "blob_url"})
        self.assertEqual(data[1]["blob_url"], "https://example/guide.pdf")

    def test_cleanup_preserves_metadata_until_other_resources_succeed(self):
        calls = []
        self.chunks.side_effect = lambda identifier: calls.append("search") or 4
        self.blobs.side_effect = lambda *args: calls.append("blob") or {"original": "deleted"}
        self.metadata.side_effect = lambda record: calls.append("metadata")
        result = service.delete_document("doc-1")
        self.assertEqual(calls, ["search", "blob", "metadata"])
        self.assertEqual(result["deleted_chunk_count"], 4)
        self.assertEqual(result["status"], "Deleted")

    def test_partial_failure_keeps_metadata_for_retry(self):
        self.blobs.side_effect = RuntimeError("storage unavailable")
        with self.assertRaises(RuntimeError):
            service.delete_document("doc-1")
        self.metadata.assert_not_called()

    def test_missing_metadata_still_cleans_search_idempotently(self):
        self.get.return_value = None
        self.chunks.return_value = 0
        result = service.delete_document("missing")
        self.assertEqual(result["metadata_cleanup"], "already_missing")
        self.assertEqual(result["blob_cleanup"]["original"], "not_locatable")
        self.blobs.assert_not_called()
        self.metadata.assert_not_called()

    def test_processing_document_is_not_deleted(self):
        for status in service.PROCESSING_STATUSES:
            self.record["status"] = status
            with self.assertRaises(ValueError):
                service.delete_document("doc-1")
        self.chunks.assert_not_called()
        self.blobs.assert_not_called()

    def test_endpoint_errors_do_not_expose_upstream_details(self):
        with patch.object(main, "delete_document", side_effect=RuntimeError("secret")):
            with self.assertRaises(main.HTTPException) as caught:
                main.remove_document("doc-1")
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn("secret", caught.exception.detail)


class BlobDeletionTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.factory = stack.enter_context(patch.object(blob_service.BlobServiceClient, "from_connection_string"))
        self.client = self.factory.return_value.__enter__.return_value
        self.client.url = "https://account.blob.core.windows.net/"
        stack.enter_context(patch.object(blob_service, "CONTAINER_NAME", "documents"))
        self.record = {"file_name": "guide.pdf", "blob_url": "https://account.blob.core.windows.net/documents/guide.pdf"}

    def test_only_original_blob_deleted_even_with_legacy_checkpoint_urls(self):
        self.record.update({"extracted_json_url": "https://retired/extracted.json",
                            "chunked_json_url": "https://retired/chunked.json"})
        result = blob_service.delete_document_blobs(self.record, [])
        self.assertEqual(result, {"original": "deleted"})
        self.client.get_blob_client.assert_called_once_with(container="documents", blob="guide.pdf")
        self.client.get_container_client.assert_not_called()

    def test_missing_original_blob_is_tolerated(self):
        self.client.get_blob_client.return_value.delete_blob.side_effect = ResourceNotFoundError()
        self.assertEqual(blob_service.delete_document_blobs(self.record, []), {"original": "already_missing"})

    def test_shared_names_are_preserved_for_other_documents(self):
        result = blob_service.delete_document_blobs(self.record, [{"file_name": "guide.pdf"}])
        self.assertTrue(all(value == "shared_preserved" for value in result.values()))
        self.client.get_blob_client.assert_not_called()

    def test_same_stem_different_extension_does_not_share_original(self):
        result = blob_service.delete_document_blobs(self.record, [{"file_name": "guide.docx"}])
        self.assertEqual(result, {"original": "deleted"})

    def test_foreign_account_or_wrong_container_rejected_before_deletion(self):
        for url in ["https://other.blob.core.windows.net/documents/guide.pdf", "https://account.blob.core.windows.net/unrelated/guide.pdf"]:
            with self.assertRaises(ValueError):
                blob_service.delete_document_blobs({**self.record, "blob_url": url}, [])
        self.client.get_blob_client.assert_not_called()


class StorageDeletionTests(unittest.TestCase):
    def test_search_filter_batching_and_acknowledgements(self):
        with patch.object(search_service, "_configuration", return_value=("https://example", None, "rag-index")), \
                patch.object(search_service, "SearchClient") as factory:
            client = factory.return_value.__enter__.return_value
            client.search.return_value = [{"chunk_id": str(i)} for i in range(1001)]
            client.delete_documents.side_effect = lambda documents: [SimpleNamespace(key=item["chunk_id"], succeeded=True) for item in documents]
            self.assertEqual(search_service.delete_document_chunks("id'one"), 1001)
            self.assertEqual(client.search.call_args.kwargs["filter"], "document_id eq 'id''one'")
            self.assertEqual([len(call.kwargs["documents"]) for call in client.delete_documents.call_args_list], [1000, 1])
            client.delete_documents.side_effect = lambda documents: []
            with self.assertRaises(RuntimeError):
                search_service.delete_document_chunks("id-one")

    def test_missing_search_index_is_safe(self):
        with patch.object(search_service, "_configuration", return_value=("https://example", None, "rag-index")), \
                patch.object(search_service, "SearchClient") as factory:
            factory.return_value.__enter__.return_value.search.side_effect = ResourceNotFoundError()
            self.assertEqual(search_service.delete_document_chunks("id-one"), 0)

    def test_cosmos_uses_document_id_partition_and_missing_record_is_safe(self):
        with patch.object(cosmos_service, "container") as container:
            cosmos_service.delete_document_record({"id": "item-id", "document_id": "doc-id"})
            container.delete_item.assert_called_once_with(item="item-id", partition_key="doc-id")
            container.delete_item.side_effect = CosmosResourceNotFoundError(status_code=404)
            cosmos_service.delete_document_record({"id": "item-id", "document_id": "doc-id"})


if __name__ == "__main__":
    unittest.main()
