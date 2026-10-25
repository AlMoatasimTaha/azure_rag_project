"""Offline retrieval checks: python -m unittest backend.test_retrieval."""

from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch

from azure.core.exceptions import HttpResponseError
from fastapi import HTTPException
from pydantic import ValidationError

with patch("azure.cosmos.CosmosClient"):
    from backend import main
from backend import search_service as service


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict("os.environ", {
            "AZURE_SEARCH_ENDPOINT": "https://example.search.windows.net",
            "AZURE_SEARCH_API_KEY": "test-only",
            "AZURE_SEARCH_INDEX_NAME": "rag-index",
        }, clear=True))
        self.factory = stack.enter_context(patch.object(service, "SearchClient"))
        self.client = self.factory.return_value.__enter__.return_value
        self.index_factory = stack.enter_context(patch.object(service, "SearchIndexClient"))
        self.vector = [0.1] * 1536
        self.match = {
            "chunk_id": "text_one", "content": "Source content", "document_id": "doc-one",
            "file_name": "example.pdf", "chunk_type": "text", "page_numbers": [2],
            "section_heading": "Section", "source_blob_url": "https://example/source.pdf",
            "@search.score": 0.9, "content_vector": self.vector,
        }
        self.client.search.return_value = [self.match]

    def test_filtered_vector_search_uses_ten_and_returns_metadata_without_vectors(self):
        results = service.search_chunks(self.vector, "doc-one")
        kwargs = self.client.search.call_args.kwargs
        self.assertIsNone(kwargs["search_text"])
        self.assertEqual(kwargs["filter"], "document_id eq 'doc-one'")
        self.assertEqual(kwargs["vector_filter_mode"], "preFilter")
        self.assertEqual(kwargs["top"], 10)
        query = kwargs["vector_queries"][0]
        self.assertEqual(query.k_nearest_neighbors, 10)
        self.assertEqual(query.fields, "content_vector")
        self.assertEqual(query.vector, self.vector)
        self.assertNotIn("content_vector", kwargs["select"])
        self.assertEqual(set(results[0]), set(service.RETRIEVAL_FIELDS) | {"score"})
        self.assertEqual(results[0]["score"], 0.9)
        self.assertEqual(results[0]["content"], "Source content")
        self.assertNotIn("content_vector", json.dumps(results))
        self.index_factory.assert_not_called()
        self.client.upload_documents.assert_not_called()

    def test_document_id_cannot_inject_an_odata_filter(self):
        service.search_chunks(self.vector, "x' or document_id ne 'x")
        self.assertEqual(self.client.search.call_args.kwargs["filter"],
                         "document_id eq 'x'' or document_id ne ''x'")

    def test_empty_results_are_successful(self):
        self.client.search.return_value = []
        self.assertEqual(service.search_chunks(self.vector, "missing"), [])

    def test_invalid_search_input_rejected_before_network(self):
        for vector, identifier, top_k in [([], "id", 3), ([0.1] * 1535, "id", 3),
                ([float("nan")] * 1536, "id", 3), ([True] * 1536, "id", 3),
                ([10**400] * 1536, "id", 3), (self.vector, " ", 3),
                (self.vector, "id", 0)]:
            with self.subTest(identifier=identifier, top_k=top_k), self.assertRaises(ValueError):
                service.search_chunks(vector, identifier, top_k)
        self.factory.assert_not_called()

    def test_paged_search_errors_are_sanitized(self):
        def pages():
            raise HttpResponseError("private upstream details")
            yield
        self.client.search.return_value = pages()
        with self.assertRaisesRegex(RuntimeError, "retrieval failed") as caught:
            service.search_chunks(self.vector, "doc-one")
        self.assertNotIn("private upstream", str(caught.exception))

    def test_request_requires_nonempty_strings(self):
        for data in [{}, {"question": " ", "document_id": "id"},
                {"question": "What?", "document_id": "\n"},
                {"question": None, "document_id": "id"},
                {"question": 123, "document_id": "id"}]:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                main.AskRequest(**data)

    def test_endpoint_reuses_embedding_and_search_with_trimmed_input(self):
        with patch.object(main, "generate_embedding", return_value=self.vector) as embed, \
                patch.object(main, "search_chunks", return_value=[]) as search:
            result = main.ask_question(main.AskRequest(question=" What is cloud? ", document_id=" doc-one "))
        embed.assert_called_once_with("What is cloud?")
        search.assert_called_once_with(self.vector, "doc-one", top_k=10)
        self.assertEqual(result, {"question": "What is cloud?", "document_id": "doc-one",
                                  "top_k": 10, "count": 0, "results": []})

    def test_embedding_failure_stops_search_and_hides_details(self):
        with patch.object(main, "generate_embedding", side_effect=RuntimeError("private details")), \
                patch.object(main, "search_chunks") as search:
            with self.assertRaises(HTTPException) as caught:
                main.ask_question(main.AskRequest(question="What?", document_id="doc-one"))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn("private details", caught.exception.detail)
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
