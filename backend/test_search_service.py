"""Offline Search service checks: python -m unittest backend.test_search_service."""

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import json
from io import StringIO
import unittest
from unittest.mock import patch

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.search.documents.models import IndexingResult

from backend import search_service as service


class SearchServiceTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict("os.environ", {
            "AZURE_SEARCH_ENDPOINT": "https://example.search.windows.net",
            "AZURE_SEARCH_API_KEY": "test-only-key",
            "AZURE_SEARCH_INDEX_NAME": "test-chunks",
        }, clear=True))
        self.index_factory = stack.enter_context(patch.object(service, "SearchIndexClient"))
        self.search_factory = stack.enter_context(patch.object(service, "SearchClient"))
        self.index = self.index_factory.return_value.__enter__.return_value
        self.search = self.search_factory.return_value.__enter__.return_value
        self.schema = service._index_schema("test-chunks", 7)
        self.index.get_index.return_value = self.schema
        self.index.create_index.side_effect = lambda schema: schema
        self.search.upload_documents.side_effect = lambda documents: [
            IndexingResult.deserialize({"key": doc["chunk_id"], "status": True, "statusCode": 201})
            for doc in documents
        ]
        self.data = {
            "document_id": "doc-1", "file_name": "example.pdf",
            "chunking": {"text_chunk_size": 400, "text_overlap": 80},
            "text_chunks": [{
                "chunk_id": "text_1", "document_id": "doc-1", "file_name": "example.pdf",
                "chunk_type": "text", "content": " original paragraph ",
                "content_vector": [0.125] * 7, "page_number": 1, "page_numbers": [1],
                "spans": [{"offset": 0, "length": 20}], "bounding_regions": [],
                "related_table_ids": ["table_1"], "section_heading": None,
                "source_blob_url": "https://example/source.pdf", "chunk_index": 0,
                "token_count": 3, "string_index_type": "unicodeCodePoint",
                "extracted_json_url": "https://example/extracted.json",
            }],
            "table_chunks": [{
                "chunk_id": "table_1", "document_id": "doc-1", "file_name": "example.pdf",
                "chunk_type": "table", "content": "Label: value", "content_vector": [0.25] * 7,
                "page_numbers": [1, 2], "table_index": 0, "row_count": 2, "column_count": 1,
                "related_text_chunk_ids": ["text_1"], "cells": [{"content": "value", "rowIndex": 1}],
                "spans": [{"offset": 20, "length": 10}],
                "bounding_regions": [{"pageNumber": 1, "polygon": [0.1, 0.2]}],
            }],
        }

    def test_direct_upload_preserves_metadata_vectors_and_input(self):
        original = deepcopy(self.data)
        with self.assertLogs(service.logger, level="INFO") as logs:
            results = service.index_chunks(self.data)
        self.assertEqual(self.data, original)
        documents = self.search.upload_documents.call_args.kwargs["documents"]
        for chunk, document in zip(self.data["text_chunks"] + self.data["table_chunks"], documents):
            restored = deepcopy(document)
            for name in service.JSON_FIELDS:
                if name in restored:
                    restored[name] = json.loads(restored[name])
            self.assertEqual(restored, {key: value for key, value in chunk.items() if key != "extracted_json_url"})
            self.assertNotIn("extracted_json_url", document)
        self.assertEqual(len(results), 2)
        self.assertTrue(any("Indexing chunk: table_1" in line for line in logs.output))
        self.assertTrue(any("Indexed successfully" in line for line in logs.output))
        self.assertFalse(any("0.125" in line for line in logs.output))
        self.index.create_index.assert_not_called()

    def test_missing_index_created_with_measured_dimension_and_hnsw(self):
        self.index.get_index.side_effect = ResourceNotFoundError()
        service.index_chunks(self.data)
        schema = self.index.create_index.call_args.args[0]
        fields = {field.name: field for field in schema.fields}
        self.assertEqual(fields["content_vector"].vector_search_dimensions, 7)
        self.assertEqual(schema.vector_search.algorithms[0].kind, "hnsw")
        serialized = schema.serialize()
        self.assertEqual([f["name"] for f in serialized["fields"] if f.get("key")], ["chunk_id"])
        self.assertTrue(fields["content"].searchable)
        self.assertFalse(fields["content"].hidden)

    def test_existing_index_wrong_dimension_or_schema_never_uploaded(self):
        for name, value in [("vector_search_dimensions", 8), ("searchable", False), ("hidden", True)]:
            with self.subTest(attribute=name):
                schema = deepcopy(self.schema)
                setattr(schema.fields[2], name, value)
                self.index.get_index.return_value = schema
                with self.assertRaises(ValueError):
                    service.index_chunks(self.data)
        schema = deepcopy(self.schema)
        schema.fields = [field for field in schema.fields if field.name != "cells"]
        self.index.get_index.return_value = schema
        with self.assertRaises(ValueError):
            service.index_chunks(self.data)
        self.search.upload_documents.assert_not_called()
        self.index.create_index.assert_not_called()

    def test_concurrent_index_creation_rechecks_schema(self):
        self.index.get_index.side_effect = [ResourceNotFoundError(), self.schema]
        self.index.create_index.side_effect = ResourceExistsError()
        self.assertEqual(len(service.index_chunks(self.data)), 2)

    def test_index_only_initialization_creates_confirmed_schema_without_uploads(self):
        self.index.get_index.side_effect = ResourceNotFoundError()
        with self.assertLogs(service.logger, level="INFO") as logs:
            schema = service.ensure_index_exists()
        fields = {field.name: field for field in schema.fields}
        self.assertEqual(fields["content_vector"].vector_search_dimensions, 1536)
        self.assertEqual(fields["content_vector"].vector_search_profile_name, "rag-vector-profile")
        self.assertEqual(schema.vector_search.algorithms[0].name, "rag-hnsw")
        self.assertEqual(schema.vector_search.algorithms[0].parameters.metric, "cosine")
        self.assertTrue(all(field.hidden is False for field in schema.fields))
        self.assertTrue(fields["section_heading"].searchable)
        for name in ("document_id", "file_name", "chunk_type", "page_numbers"):
            self.assertTrue(fields[name].filterable)
        self.assertTrue(any("Index created successfully" in line for line in logs.output))
        self.search_factory.assert_not_called()

    def test_index_only_rerun_does_not_recreate_or_upload(self):
        with patch.dict("os.environ", {"AZURE_SEARCH_INDEX_NAME": "rag-index"}):
            self.index.get_index.return_value = service._index_schema("rag-index", 1536)
            output = StringIO()
            with redirect_stdout(output):
                service.ensure_index_exists()
        self.assertEqual(output.getvalue(), "Index rag-index already exists\n")
        self.index.create_index.assert_not_called()
        self.search_factory.assert_not_called()

    def test_invalid_chunks_rejected_before_network_access(self):
        cases = [("chunk_id", None), ("chunk_id", "bad/key"), ("content", "  "),
                 ("content_vector", None), ("content_vector", []), ("content_vector", [True]),
                 ("content_vector", ["1"]), ("content_vector", [float("nan")]),
                 ("content_vector", [float("inf")]), ("content_vector", [10**400]),
                 ("content_vector", [0.1] * 6)]
        for name, value in cases:
            data = deepcopy(self.data)
            data["table_chunks"][0][name] = value
            with self.subTest(field=name), self.assertRaises(ValueError):
                service.index_chunks(data)
        data = deepcopy(self.data)
        data["table_chunks"][0]["chunk_id"] = "text_1"
        with self.assertRaises(ValueError):
            service.index_chunks(data)
        self.index_factory.assert_not_called()
        self.search_factory.assert_not_called()

    def test_partial_failure_not_reported_as_success(self):
        self.search.upload_documents.side_effect = None
        self.search.upload_documents.return_value = [
            IndexingResult.deserialize({"key": "text_1", "status": True, "statusCode": 201}),
            IndexingResult.deserialize({"key": "table_1", "status": False, "statusCode": 400,
                                       "errorMessage": "private detail"}),
        ]
        with self.assertRaisesRegex(RuntimeError, "did not index every chunk") as caught:
            service.index_chunks(self.data)
        self.assertNotIn("private detail", str(caught.exception))

    def test_api_errors_sanitized(self):
        self.index.get_index.side_effect = HttpResponseError("private detail")
        with self.assertRaisesRegex(RuntimeError, "index setup failed") as caught:
            service.index_chunks(self.data)
        self.assertNotIn("private detail", str(caught.exception))
        self.index.get_index.side_effect = None
        self.search.upload_documents.side_effect = HttpResponseError("private detail")
        with self.assertRaisesRegex(RuntimeError, "upload failed") as caught:
            service.index_chunks(self.data)
        self.assertNotIn("private detail", str(caught.exception))

    def test_empty_input_and_missing_configuration(self):
        self.assertEqual(service.index_chunks({"text_chunks": [], "table_chunks": []}), [])
        self.index_factory.assert_not_called()
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "AZURE_SEARCH_API_KEY"):
                service.index_chunks(self.data)

    def test_batches_obey_document_and_byte_limits(self):
        documents = [{"chunk_id": str(i), "content": "x"} for i in range(1001)]
        self.assertEqual([len(batch) for batch in service._batches(documents)], [1000, 1])
        documents = [{"content": "x" * (8 * 1024 * 1024)}] * 2
        self.assertEqual([len(batch) for batch in service._batches(documents)], [1, 1])
        with self.assertRaises(ValueError):
            list(service._batches([{"content": "x" * (16 * 1024 * 1024)}]))


if __name__ == "__main__":
    unittest.main()
