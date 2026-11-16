"""Offline checks for grounded generation and the /ask integration."""

import importlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openai import OpenAIError

from backend import llm_service


CONFIG = {
    "AZURE_OPENAI_ENDPOINT": "https://example.invalid/openai/v1/",
    "AZURE_OPENAI_API_KEY": "test-secret",
    "AZURE_OPENAI_DEPLOYMENT": "test-gpt5-mini",
}
CHUNKS = [
    {"content": "Alpha costs 10.", "file_name": "guide.pdf",
     "section_heading": "Pricing", "page_numbers": [2, 3]},
    {"content": "Beta costs 20."},
    {"content": "Gamma costs 30."},
    {"content": "Delta costs 40."},
]


class GenerationTests(unittest.TestCase):
    def test_context_and_responses_call(self):
        with patch.dict(os.environ, CONFIG), patch.object(llm_service, "OpenAI") as factory:
            client = factory.return_value.__enter__.return_value
            client.responses.create.return_value = SimpleNamespace(output_text="  Alpha costs 10.  ")
            self.assertEqual(llm_service.generate_answer("What does Alpha cost?", CHUNKS), "Alpha costs 10.")
            factory.assert_called_once_with(
                api_key=CONFIG["AZURE_OPENAI_API_KEY"], base_url=CONFIG["AZURE_OPENAI_ENDPOINT"],
                timeout=60.0, max_retries=0,
            )
            call = client.responses.create.call_args.kwargs
            self.assertEqual(call["model"], CONFIG["AZURE_OPENAI_DEPLOYMENT"])
            self.assertEqual(call["instructions"], llm_service.GROUNDING_INSTRUCTIONS)
            self.assertIn("[Chunk 1]\nFile: guide.pdf\nSection: Pricing\nPages: 2, 3\nContent: Alpha costs 10.", call["input"])
            self.assertIn("[Chunk 4]\nContent: Delta costs 40.", call["input"])
            self.assertIn("User question:\nWhat does Alpha cost?", call["input"])
            self.assertNotIn("test-secret", call["input"])

    def test_no_context_does_not_create_client(self):
        with patch.object(llm_service, "OpenAI") as factory:
            for chunks in ([], [{"content": " "}], [{"content": None}]):
                self.assertEqual(llm_service.generate_answer("Question", chunks), llm_service.NO_CONTEXT_ANSWER)
            factory.assert_not_called()

    def test_missing_configuration(self):
        for name in CONFIG:
            with self.subTest(name=name), patch.dict(os.environ, {**CONFIG, name: ""}):
                with self.assertRaises(llm_service.LLMConfigurationError) as caught:
                    llm_service.generate_answer("Question", CHUNKS)
                self.assertIn(name, str(caught.exception))
                self.assertNotIn("test-secret", str(caught.exception))

    def test_invalid_endpoint(self):
        with patch.dict(os.environ, {**CONFIG, "AZURE_OPENAI_ENDPOINT": "https://example.invalid"}):
            with self.assertRaises(llm_service.LLMConfigurationError):
                llm_service.generate_answer("Question", CHUNKS)

    def test_api_failure_is_sanitized(self):
        with patch.dict(os.environ, CONFIG), patch.object(llm_service, "OpenAI") as factory:
            factory.return_value.__enter__.return_value.responses.create.side_effect = OpenAIError("test-secret raw response")
            with self.assertRaises(llm_service.LLMGenerationError) as caught:
                llm_service.generate_answer("Question", CHUNKS)
            self.assertNotIn("test-secret", str(caught.exception))
            self.assertIn("request failed", str(caught.exception))

    def test_empty_model_response(self):
        for answer in (None, "", "  "):
            with self.subTest(answer=answer), patch.dict(os.environ, CONFIG), patch.object(llm_service, "OpenAI") as factory:
                factory.return_value.__enter__.return_value.responses.create.return_value = SimpleNamespace(output_text=answer)
                with self.assertRaisesRegex(llm_service.LLMGenerationError, "empty answer"):
                    llm_service.generate_answer("Question", CHUNKS)


class AskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The existing Cosmos module creates a client on import. Keep tests offline.
        with patch("azure.cosmos.CosmosClient"):
            cls.main = importlib.import_module("backend.main")

    def setUp(self):
        patcher = patch.object(self.main, "list_documents", return_value=[{"document_id": "doc-1", "status": "Ready"}])
        self.ready_documents = patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_ready_documents_share_one_top_four_search(self):
        self.ready_documents.return_value = [
            {"document_id": "doc-1", "status": "Ready"},
            {"document_id": "doc-2", "status": "Ready"},
            {"document_id": "unfinished", "status": "Embedding"},
            {"document_id": "failed", "status": "Failed"},
        ]
        with patch.object(self.main, "generate_embedding", return_value=[0.0] * 1536), patch.object(self.main, "search_chunks", return_value=CHUNKS) as search, patch.object(self.main, "generate_answer", return_value="Combined answer") as generate:
            response = self.main.ask_question(self.main.AskRequest(question="Compare the documents"))
        search.assert_called_once_with([0.0] * 1536, document_ids=["doc-1", "doc-2"], top_k=4)
        generate.assert_called_once_with("Compare the documents", CHUNKS)
        self.assertIsNone(response["document_id"])
        self.assertEqual(response["answer"], "Combined answer")

    def test_no_ready_or_unavailable_selected_document_never_calls_model(self):
        from fastapi import HTTPException
        for ready, selected in [([], None), ([{"document_id": "other", "status": "Ready"}], "doc-1")]:
            self.ready_documents.return_value = ready
            with patch.object(self.main, "generate_embedding") as embed, patch.object(self.main, "generate_answer") as generate:
                with self.assertRaises(HTTPException) as error:
                    self.main.ask_question(self.main.AskRequest(question="Question", document_id=selected))
                self.assertEqual(error.exception.status_code, 409)
                embed.assert_not_called()
                generate.assert_not_called()

    def test_all_twenty_ready_documents_are_included_in_scope(self):
        ids = [f"ready-{index}" for index in range(20)]
        self.ready_documents.return_value = [{"document_id": key, "status": "Ready"} for key in ids]
        with patch.object(self.main, "generate_embedding", return_value=[0.0] * 1536), patch.object(self.main, "search_chunks", return_value=CHUNKS) as search, patch.object(self.main, "generate_answer", return_value="Answer"):
            response = self.main.ask_question(self.main.AskRequest(question="Question across all documents"))
        self.assertEqual(search.call_args.kwargs["document_ids"], ids)
        self.assertEqual(response["top_k"], 4)

    def test_existing_retrieval_and_response_are_preserved(self):
        request = self.main.AskRequest(question="Question", document_id="doc-1")
        with patch.object(self.main, "generate_embedding", return_value=[0.0] * 1536) as embed, patch.object(self.main, "search_chunks", return_value=CHUNKS) as search, patch.object(self.main, "generate_answer", return_value="Answer") as generate:
            result = self.main.ask_question(request)
        embed.assert_called_once_with("Question")
        search.assert_called_once_with([0.0] * 1536, "doc-1", top_k=4)
        generate.assert_called_once_with("Question", CHUNKS)
        self.assertEqual(result, {"question": "Question", "document_id": "doc-1",
                                 "top_k": 4, "count": 4, "results": CHUNKS, "answer": "Answer"})

    def test_empty_retrieval_skips_model(self):
        with patch.object(self.main, "generate_embedding", return_value=[0.0] * 1536), patch.object(self.main, "search_chunks", return_value=[]), patch.object(llm_service, "OpenAI") as factory:
            result = self.main.ask_question(self.main.AskRequest(question="Question", document_id="doc-1"))
        self.assertEqual(result["answer"], llm_service.NO_CONTEXT_ANSWER)
        self.assertEqual(result["count"], 0)
        factory.assert_not_called()

    def test_generation_errors_have_clear_http_status(self):
        cases = [(llm_service.LLMConfigurationError("Missing configuration"), 503),
                 (llm_service.LLMGenerationError("Empty answer"), 502),
                 (RuntimeError("test-secret"), 502)]
        for error, status in cases:
            with self.subTest(status=status), patch.object(self.main, "generate_embedding", return_value=[0.0] * 1536), patch.object(self.main, "search_chunks", return_value=CHUNKS), patch.object(self.main, "generate_answer", side_effect=error):
                with self.assertRaises(self.main.HTTPException) as caught:
                    self.main.ask_question(self.main.AskRequest(question="Question", document_id="doc-1"))
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn("test-secret", caught.exception.detail)


class CollectionSearchTests(unittest.TestCase):
    def test_multiple_documents_use_one_filtered_global_top_four_query(self):
        from backend import search_service
        with patch.object(search_service, "_configuration", return_value=("https://example.invalid", "credential", "index")), patch.object(search_service, "SearchClient") as factory:
            client = factory.return_value.__enter__.return_value
            client.search.return_value = [
                {"chunk_id": "a", "document_id": "doc-1", "file_name": "one.pdf", "content": "First", "@search.score": 0.9},
                {"chunk_id": "b", "document_id": "doc'2", "file_name": "two.pdf", "content": "Second", "@search.score": 0.8},
            ]
            results = search_service.search_chunks([0.0] * 1536, document_ids=["doc-1", "doc'2", "doc-1"])
            client.search.assert_called_once()
            args = client.search.call_args.kwargs
            self.assertEqual(args["filter"], "document_id eq 'doc-1' or document_id eq 'doc''2'")
            self.assertEqual(args["top"], 4)
            self.assertEqual(args["vector_queries"][0].k_nearest_neighbors, 4)
            self.assertEqual(args["vector_filter_mode"], "preFilter")
            self.assertEqual([item["document_id"] for item in results], ["doc-1", "doc'2"])

    def test_empty_document_scope_never_searches_whole_index(self):
        from backend import search_service
        with patch.object(search_service, "SearchClient") as factory:
            self.assertEqual(search_service.search_chunks([0.0] * 1536, document_ids=[]), [])
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
