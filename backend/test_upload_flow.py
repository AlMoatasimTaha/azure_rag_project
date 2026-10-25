"""Offline upload orchestration tests: python -m unittest backend.test_upload_flow."""

from contextlib import ExitStack
from copy import deepcopy
import json
from tempfile import SpooledTemporaryFile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import UploadFile

# cosmos_service constructs its client at import time. Prevent network access
# while importing the real endpoint; each service call below is mocked as well.
with patch("azure.cosmos.CosmosClient"):
    from backend import main
from backend import extraction_service, chunking_service


class UploadFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.validate = stack.enter_context(patch.object(main, "validate_document", return_value=(True, None)))
        self.upload = stack.enter_context(patch.object(main, "upload_to_blob", return_value="https://example/source.pdf"))
        self.extract = stack.enter_context(patch.object(main, "extract_document"))
        self.chunk = stack.enter_context(patch.object(main, "chunk_document"))
        self.embed = stack.enter_context(patch.object(main, "embed_chunks"))
        self.index = stack.enter_context(patch.object(main, "index_chunks"))
        self.records = []
        self.save_record = stack.enter_context(patch.object(
            main, "save_document_result", side_effect=lambda record: self.records.append(deepcopy(record))))
        self.extracted = {
            "file_name": "example.pdf", "blob_url": self.upload.return_value,
            "status": "Extracted",
            "original_page_count": 2, "extracted_page_count": 2, "error_reason": None,
            "extraction_result": {"content": "real extraction would be here", "pages": []},
        }
        self.chunked = {
            "file_name": "example.pdf", "chunking": {"text_chunk_size": 400, "text_overlap": 80},
            "text_chunks": [{"content": "one"}, {"content": "two"}], "table_chunks": [{"content": "table"}],
        }

        def extract(*args, **kwargs):
            self.assertEqual(self.records[-1]["status"], "Extracting")
            self.assertTrue(kwargs["include_extraction_result"])
            return {**deepcopy(self.extracted), "document_id": kwargs["document_id"]}

        def chunk(document):
            self.assertEqual(self.records[-1]["status"], "Chunking")
            self.assertIn("extraction_result", document)
            self.chunked_result = deepcopy(self.chunked)
            return self.chunked_result

        self.extract.side_effect = extract
        self.chunk.side_effect = chunk

        def embed(document):
            self.assertEqual(self.records[-1]["status"], "Embedding")
            self.assertIs(document, self.chunked_result)
            result = deepcopy(document)
            for group in ("text_chunks", "table_chunks"):
                for item in result[group]:
                    item["content_vector"] = [0.1, 0.2]
            self.embedded_result = result
            return result

        self.embed.side_effect = embed

        def index(document):
            self.assertEqual(self.records[-1]["status"], "Indexing")
            self.assertIs(document, self.embedded_result)
            self.assertNotIn("Ready", self.statuses())
            return [SimpleNamespace(succeeded=True) for group in ("text_chunks", "table_chunks") for item in document[group]]

        self.index.side_effect = index

    async def run_upload(self, progress=False):
        stream = SpooledTemporaryFile(max_size=1024)
        stream.write(b"file bytes")
        stream.seek(0)
        file = UploadFile(filename="example.pdf", file=stream)
        try:
            return await main.upload_document(file, progress=progress)
        finally:
            await file.close()

    def statuses(self):
        return [record["status"] for record in self.records]

    async def test_success_updates_one_record_and_preserves_metadata(self):
        response = await self.run_upload()
        self.assertEqual(self.statuses(), ["Valid", "Extracting", "Chunking", "Embedding", "Indexing", "Ready"])
        self.assertEqual(len({record["id"] for record in self.records}), 1)
        final = self.records[-1]
        for key, value in self.records[0].items():
            if key != "status":
                self.assertEqual(final[key], value)
        self.assertEqual(final["original_page_count"], 2)
        self.assertEqual(final["extracted_page_count"], 2)
        self.assertEqual(final["text_chunk_count"], 2)
        self.assertEqual(final["table_chunk_count"], 1)
        self.assertEqual(final.get("indexed_chunk_count", 0), 3 if final["status"] == "Ready" else 0)
        self.assertEqual((final["chunk_size"], final["chunk_overlap"]), (400, 80))
        self.assertTrue(final["extracted_at"])
        self.assertTrue(final["chunked_at"])
        self.assertTrue(final["embedded_at"])
        self.assertTrue(final["indexed_at"])
        self.assertIsNone(final["error_reason"])
        for record in self.records:
            for field in ("extraction_result", "text_chunks", "table_chunks"):
                self.assertNotIn(field, record)
        self.assertEqual(response["status"], "Ready")
        self.index.assert_called_once()
        self.assertNotIn("content_vector", json.dumps(self.records))
        self.assertNotIn("content_vector", json.dumps(response))

    async def test_invalid_document_stops_before_all_processing(self):
        self.validate.return_value = False, "Invalid document"
        response = await self.run_upload()
        self.assertEqual(response["status"], "Invalid")
        self.assertEqual(response["message"], "Invalid document")
        for mocked in (self.upload, self.extract, self.chunk, self.embed, self.index, self.save_record):
            mocked.assert_not_called()

    async def test_extraction_failure_does_not_chunk(self):
        self.extract.side_effect = RuntimeError("Extraction unavailable")
        response = await self.run_upload()
        self.assertEqual(self.statuses(), ["Valid", "Extracting", "Failed"])
        self.assertEqual(response["status"], "Failed")
        self.assertIn("extraction", self.records[-1]["error_reason"])
        self.chunk.assert_not_called()

    async def test_chunk_failure_preserves_extraction_and_records_reason(self):
        self.chunk.side_effect = ValueError("Missing page spans")
        response = await self.run_upload()
        self.assertEqual(self.statuses(), ["Valid", "Extracting", "Chunking", "Failed"])
        self.assertEqual(response["status"], "Failed")
        self.assertIn("Missing page spans", self.records[-1]["error_reason"])
        self.assertEqual(self.records[-1]["original_page_count"], 2)
        self.assertNotIn("chunked_at", self.records[-1])

    async def test_no_intermediate_blob_access_or_checkpoint_urls(self):
        with patch("azure.storage.blob.BlobServiceClient.from_connection_string",
                   side_effect=AssertionError("Unexpected intermediate Blob access")) as storage:
            response = await self.run_upload()
        self.assertEqual(response["status"], "Ready")
        storage.assert_not_called()
        for record in [response, *self.records]:
            self.assertNotIn("extracted_json_url", record)
            self.assertNotIn("chunked_json_url", record)
            self.assertNotIn("checkpoint_warnings", record)
        self.upload.assert_called_once()
        self.embed.assert_called_once_with(self.chunked_result)
        self.index.assert_called_once_with(self.embedded_result)

    async def test_upload_failure_is_recorded_and_stops_later_stages(self):
        self.upload.side_effect = RuntimeError("Private storage details")
        response = await self.run_upload()
        self.assertEqual(self.statuses(), ["Valid", "Failed"])
        self.assertEqual(response["failed_stage"], "Upload")
        self.assertNotIn("Private storage details", json.dumps(response))
        self.extract.assert_not_called()

    async def test_cosmos_failure_at_every_transition_stops_pipeline(self):
        for status in ("Valid", "Extracting", "Chunking", "Embedding", "Indexing", "Ready"):
            with self.subTest(status=status):
                self.records.clear()
                def save(record):
                    if record["status"] == status:
                        raise PermissionError("Private Cosmos detail")
                    self.records.append(deepcopy(record))
                self.save_record.side_effect = save
                response = await self.run_upload()
                self.assertEqual(response["status"], "Failed")
                self.assertEqual(self.records[-1]["status"], "Failed")
                self.assertNotIn("Ready", self.statuses())
                self.assertIn("Cosmos", response["error_reason"])
                self.assertNotIn("Private Cosmos detail", json.dumps(response))

    async def test_failed_status_write_failure_is_contained(self):
        self.save_record.side_effect = PermissionError("Private Cosmos detail")
        response = await self.run_upload()
        self.assertEqual(response["status"], "Failed")
        self.assertIn("status_persistence_error", response)
        self.assertNotIn("Private Cosmos detail", json.dumps(response))
        self.extract.assert_not_called()

    async def test_missing_upload_acknowledgements_never_mark_ready(self):
        self.index.side_effect = lambda document: []
        response = await self.run_upload()
        self.assertEqual(response["status"], "Failed")
        self.assertEqual(response["failed_stage"], "Indexing")
        self.assertNotIn("Ready", self.statuses())

    async def test_embedding_failure_preserves_metadata_and_reports_failed(self):
        self.embed.side_effect = RuntimeError("Azure OpenAI embedding request failed (HTTP 429)")
        response = await self.run_upload()
        self.assertEqual(self.statuses(), ["Valid", "Extracting", "Chunking", "Embedding", "Failed"])
        self.assertEqual(response["status"], "Failed")
        self.assertIn("HTTP 429", response["error_reason"])
        self.assertEqual(response["error_reason"], self.records[-1]["error_reason"])
        self.assertEqual(self.records[-1]["original_page_count"], 2)
        self.assertNotIn("embedded_at", self.records[-1])
        self.index.assert_not_called()

    async def test_unexpected_embedding_failure_hides_private_details(self):
        self.embed.side_effect = PermissionError("Private upstream detail")
        response = await self.run_upload()
        self.assertEqual(response["status"], "Failed")
        self.assertIn("PermissionError", response["error_reason"])
        self.assertNotIn("Private upstream detail", response["error_reason"])
        self.index.assert_not_called()

    async def test_stream_embedding_failure_reports_failed_final_event(self):
        self.embed.side_effect = ValueError("Missing embedding configuration: AZURE_OPENAI_ENDPOINT")
        response = await self.run_upload(progress=True)
        events = [json.loads(update) async for update in response.body_iterator]
        self.assertEqual([event["status"] for event in events], ["Extracting", "Chunking", "Embedding", "Failed"])
        self.assertIn("AZURE_OPENAI_ENDPOINT", events[-1]["error_reason"])
        self.index.assert_not_called()

    async def test_stream_reports_real_stages_before_running_next_stage(self):
        response = await self.run_upload(progress=True)
        self.assertEqual(response.media_type, "application/x-ndjson")
        self.extract.assert_not_called()
        updates = response.body_iterator
        extracting = json.loads(await anext(updates))
        self.assertEqual(extracting["status"], "Extracting")
        self.extract.assert_not_called()
        chunking = json.loads(await anext(updates))
        self.assertEqual(chunking["status"], "Chunking")
        self.extract.assert_called_once()
        self.chunk.assert_not_called()
        embedding = json.loads(await anext(updates))
        self.assertEqual(embedding["status"], "Embedding")
        self.embed.assert_not_called()
        indexing = json.loads(await anext(updates))
        self.assertEqual(indexing["status"], "Indexing")
        self.embed.assert_called_once()
        self.index.assert_not_called()
        final = json.loads(await anext(updates))
        self.assertEqual(final["status"], "Ready")
        self.index.assert_called_once()
        self.assertEqual(self.statuses(), ["Valid", "Extracting", "Chunking", "Embedding", "Indexing", "Ready"])
        with self.assertRaises(StopAsyncIteration):
            await anext(updates)

    async def test_indexing_failure_keeps_metadata_and_never_marks_ready(self):
        self.index.side_effect = RuntimeError(
            "Azure AI Search did not index every chunk successfully; some chunks may already be indexed"
        )
        response = await self.run_upload()
        self.assertEqual(self.statuses(), ["Valid", "Extracting", "Chunking", "Embedding", "Indexing", "Failed"])
        self.assertEqual(response["status"], "Failed")
        self.assertIn("Indexing chunks in Azure AI Search failed", response["error_reason"])
        self.assertIn("some chunks may already be indexed", response["error_reason"])
        final = self.records[-1]
        self.assertEqual(final["error_reason"], response["error_reason"])
        self.assertEqual(final["text_chunk_count"], 2)
        self.assertTrue(final["embedded_at"])
        self.assertNotIn("indexed_at", final)
        self.assertNotIn("content_vector", json.dumps(self.records))
        self.assertNotIn("content_vector", json.dumps(response))
        self.index.assert_called_once_with(self.embedded_result)

    async def test_unexpected_indexing_failure_hides_private_details(self):
        self.index.side_effect = PermissionError("Private Search connection detail")
        response = await self.run_upload()
        self.assertEqual(response["status"], "Failed")
        self.assertEqual(self.records[-1]["status"], "Failed")
        self.assertIn("PermissionError", response["error_reason"])
        self.assertNotIn("Private Search connection detail", response["error_reason"])
        self.assertNotIn("Ready", self.statuses())

    async def test_stream_indexing_failure_reports_failed_final_event(self):
        self.index.side_effect = ValueError("Existing Search index vector dimension/configuration does not match embeddings")
        response = await self.run_upload(progress=True)
        events = [json.loads(update) async for update in response.body_iterator]
        self.assertEqual([event["status"] for event in events], ["Extracting", "Chunking", "Embedding", "Indexing", "Failed"])
        self.assertIn("does not match embeddings", events[-1]["error_reason"])
        self.assertEqual(self.records[-1]["status"], "Failed")
        self.assertNotIn("indexed_at", self.records[-1])

    async def test_stream_invalid_document_stops_before_processing(self):
        self.validate.return_value = False, "Invalid document"
        response = await self.run_upload(progress=True)
        events = [json.loads(update) async for update in response.body_iterator]
        self.assertEqual([event["status"] for event in events], ["Invalid"])
        self.upload.assert_not_called()
        self.extract.assert_not_called()

    async def test_stream_chunk_failure_reports_failed_final_event(self):
        self.chunk.side_effect = ValueError("Missing page spans")
        response = await self.run_upload(progress=True)
        events = [json.loads(update) async for update in response.body_iterator]
        self.assertEqual([event["status"] for event in events], ["Extracting", "Chunking", "Failed"])
        self.assertIn("Missing page spans", events[-1]["message"])

    async def test_stream_unexpected_failure_reports_failed_final_event(self):
        self.upload.side_effect = RuntimeError("Private connection detail")
        response = await self.run_upload(progress=True)
        events = [json.loads(update) async for update in response.body_iterator]
        self.assertEqual([event["status"] for event in events], ["Failed"])
        self.assertNotIn("Private connection detail", events[-1]["message"])


class ExtractionHandoffTests(unittest.TestCase):
    def test_extraction_returns_memory_result_without_storage_configuration(self):
        with patch.dict("os.environ", {
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://example.invalid",
            "AZURE_DOCUMENT_INTELLIGENCE_KEY": "test-only",
        }, clear=True), patch.object(extraction_service, "DocumentIntelligenceClient") as intelligence, \
                patch("azure.storage.blob.BlobServiceClient.from_connection_string") as storage:
            analyzed = {"content": "Hello", "stringIndexType": "unicodeCodePoint", "pages": []}
            intelligence.return_value.begin_analyze_document.return_value.result.return_value.as_dict.return_value = analyzed
            envelope = extraction_service.extract_document(
                "example.docx", b"document", document_id="document-1", blob_url="https://example/source",
                include_extraction_result=True,
            )
            self.assertIs(envelope["extraction_result"], analyzed)
            self.assertNotIn("extracted_json_url", envelope)
            metadata = extraction_service.extract_document(
                "example.docx", b"document", document_id="document-1", blob_url="https://example/source",
            )
            self.assertEqual(metadata, {key: value for key, value in envelope.items() if key != "extraction_result"})
            storage.assert_not_called()

    def test_chunking_preserves_metadata_without_checkpoint_urls(self):
        text = "Hello from this document"
        envelope = {
            "document_id": "doc-1", "file_name": "example.pdf", "blob_url": "https://example/source",
            "extraction_result": {"content": text, "stringIndexType": "unicodeCodePoint", "pages": [
                {"pageNumber": 1, "spans": [{"offset": 0, "length": len(text)}]},
            ]},
        }
        with patch.object(chunking_service, "_text_windows", return_value=[(0, len(text), text)]), \
                patch.object(chunking_service, "count_tokens", return_value=4), \
                patch("azure.storage.blob.BlobServiceClient.from_connection_string") as storage:
            chunked = chunking_service.chunk_document(envelope)
        chunk = chunked["text_chunks"][0]
        self.assertEqual(chunk["document_id"], "doc-1")
        self.assertEqual(chunk["content"], text)
        self.assertEqual(chunk["page_numbers"], [1])
        self.assertEqual(chunk["source_blob_url"], envelope["blob_url"])
        self.assertEqual(chunk["spans"], [{"offset": 0, "length": len(text)}])
        self.assertNotIn("extracted_json_url", chunk)
        storage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
