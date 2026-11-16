"""Offline deletion checks. Every Azure client and write is mocked."""
from contextlib import ExitStack
from copy import deepcopy
import importlib
from types import SimpleNamespace
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceNotFoundError


class DeletionTests(unittest.TestCase):
    def setUp(self):
        # Outbox files must never be visible to the running application's retry worker.
        from backend import cleanup_journal
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = patch.object(cleanup_journal, "CLEANUP_DIR", Path(temporary.name))
        directory.start()
        self.addCleanup(directory.stop)

    @classmethod
    def setUpClass(cls):
        with patch("azure.cosmos.CosmosClient"):
            cls.service = importlib.import_module("backend.document_service")
            cls.blobs = importlib.import_module("backend.blob_service")
            cls.search = importlib.import_module("backend.search_service")
            cls.main = importlib.import_module("backend.main")

    def record(self, key="disposable-one", **extra):
        return {"id": key, "document_id": key, "file_name": "same.pdf", "status": "Ready",
                "blob_url": f"https://test.blob.core.windows.net/originals/{key}_same.pdf", **extra}

    def coordinator(self, records, *, failure=None):
        """In-memory Azure state that actually loses chunks, files, slots and metadata."""
        state = {r["document_id"]: deepcopy(r) for r in records}
        chunks, blobs, slots = set(state), set(state), set(state)
        events = []
        def remove(kind, values, key):
            events.append((kind, key))
            if failure and failure[0] == kind and failure[1] == key:
                raise RuntimeError("simulated service failure")
            values.discard(key)
        def delete_blobs(record, others):
            remove("blobs", blobs, record["document_id"])
            return {"original": "deleted"}
        def metadata(record):
            remove("metadata", set(), record["document_id"])
            state.pop(record["document_id"], None)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, replacement in {
            "get_document_record": lambda key: deepcopy(state.get(key)),
            "list_document_records": lambda: list(deepcopy(state).values()),
            "save_document_result": lambda record: state.update({record["document_id"]: deepcopy(record)}),
            "delete_document_chunks": lambda key: remove("chunks", chunks, key) or 1,
            "delete_document_blobs": delete_blobs,
            "release_document_slot": lambda key: remove("capacity", slots, key),
            "delete_document_record": metadata,
        }.items():
            stack.enter_context(patch.object(self.service, name, side_effect=replacement))
        return state, chunks, blobs, slots, events

    def test_selected_ids_only_and_no_stale_data(self):
        state, chunks, blobs, slots, events = self.coordinator([self.record(), self.record("disposable-two")])
        result = self.service.delete_documents(["disposable-one", "disposable-one"])
        self.assertEqual(result["deleted_count"], 1)
        for store in (state, chunks, blobs, slots):
            self.assertEqual(set(store), {"disposable-two"})
        self.assertEqual([kind for kind, _ in events], ["chunks", "blobs", "capacity", "metadata"])

    def test_delete_all_partial_failure_then_retry(self):
        state, chunks, blobs, slots, _ = self.coordinator([self.record(), self.record("disposable-two")],
                                                        failure=("blobs", "disposable-one"))
        result = self.service.delete_documents()
        self.assertEqual((result["deleted_count"], result["failed_count"]), (1, 1))
        self.assertEqual(state["disposable-one"]["status"], "Deleting")
        self.assertIn("disposable-one", slots)
        # Resume the exact same record after the failed service recovers.
        with patch.object(self.service, "delete_document_blobs", side_effect=lambda record, others: blobs.discard(record["document_id"]) or {}):
            self.assertEqual(self.service.delete_documents()["failed_count"], 0)
        for store in (state, chunks, blobs, slots):
            self.assertFalse(store)

    def test_every_failure_preserves_retry_metadata(self):
        for stage in ("chunks", "blobs", "capacity", "metadata"):
            with self.subTest(stage=stage):
                state, *_ = self.coordinator([self.record()], failure=(stage, "disposable-one"))
                result = self.service.delete_documents(["disposable-one"])
                self.assertEqual(result["deleted_count"], 0)
                self.assertEqual(result["failed_count"], 1)
                self.assertIn("disposable-one", state)

    def test_processing_document_is_untouched(self):
        state, _, _, _, events = self.coordinator([self.record(status="Embedding")])
        self.assertEqual(self.service.delete_documents()["failed_count"], 1)
        self.assertFalse(events)
        self.assertEqual(state["disposable-one"]["status"], "Embedding")

    def test_missing_original_still_lists_cleanup_record(self):
        self.coordinator([self.record(status="Deleting")])
        with patch.object(self.service, "original_document_exists", return_value=False):
            result = self.service.list_documents()
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["cleanup_pending"])
        self.assertFalse(result[0]["original_available"])

    def test_missing_metadata_is_idempotent(self):
        self.coordinator([])
        result = self.service.delete_document("disposable-one")
        self.assertEqual(result["status"], "Deleted")
        self.assertEqual(result["metadata_cleanup"], "already_missing")

    def test_missing_original_is_visible_for_cleanup_but_not_a_ready_scope(self):
        self.coordinator([self.record()])
        with patch.object(self.service, "original_document_exists", return_value=False):
            result = self.service.list_documents()
        self.assertEqual(result[0]["status"], "Unavailable")

    def test_failed_upload_removes_partial_resources_without_touching_another_id(self):
        record = self.record(status="Failed", failed_stage="Indexing", extraction_result={"sensitive": "content"})
        state, chunks, blobs, slots, _ = self.coordinator([record, self.record("disposable-two")])
        self.assertTrue(self.service.cleanup_failed_upload(record))
        for store in (state, chunks, blobs, slots):
            self.assertEqual(set(store), {"disposable-two"})

    def test_actual_pipeline_extraction_failure_invokes_cleanup(self):
        state, chunks, blobs, slots, _ = self.coordinator([])
        slots.add("pipeline-fixture")
        def uploaded(*args, **kwargs):
            blobs.add(kwargs["document_id"])
            return "https://test.blob.core.windows.net/originals/pipeline-fixture_fixture.pdf"
        with patch.object(self.main, "validate_document", return_value=(True, None)), \
                patch.object(self.main, "save_document_result", side_effect=lambda record: state.update({record["document_id"]: deepcopy(record)})), \
                patch.object(self.main, "upload_to_blob", side_effect=uploaded), \
                patch.object(self.main, "extract_document", side_effect=RuntimeError("private response body")):
            results = list(self.main._process_document("fixture.pdf", b"fixture", document_id="pipeline-fixture"))
        self.assertEqual(results[-1]["status"], "Failed")
        self.assertFalse(results[-1]["cleanup_pending"])
        self.assertIn("Document extraction failed", results[-1]["message"])
        self.assertNotIn("private response body", results[-1]["message"])
        for store in (state, chunks, blobs, slots):
            self.assertFalse(store)

    def test_all_later_pipeline_failures_compensate_created_resources(self):
        for failed_operation in ("chunk_document", "embed_chunks", "index_chunks", "ready_metadata", "initial_metadata"):
            with self.subTest(operation=failed_operation):
                state, chunks, blobs, slots, _ = self.coordinator([])
                key = "pipeline-fixture"
                slots.add(key)
                def save(record):
                    state[record["document_id"]] = deepcopy(record)
                    if (failed_operation == "ready_metadata" and record["status"] == "Ready"
                            or failed_operation == "initial_metadata" and record["status"] == "Valid"):
                        raise RuntimeError("acknowledgement lost after commit")
                def upload(*args, **kwargs):
                    blobs.add(key)
                    return "https://test.blob.core.windows.net/originals/pipeline-fixture_fixture.pdf"
                def index(*args):
                    chunks.add(key)  # Includes partial writes before a failed response.
                    if failed_operation == "index_chunks":
                        raise RuntimeError("Search did not confirm all chunks")
                    return [SimpleNamespace(succeeded=True)]
                with ExitStack() as stack:
                    replacements = {"validate_document": lambda *args: (True, None), "save_document_result": save,
                                    "upload_to_blob": upload, "extract_document": lambda *args, **kwargs: {},
                                    "chunk_document": lambda *args: {"text_chunks": [{}], "table_chunks": [], "chunking": {"text_chunk_size": 1, "text_overlap": 0}},
                                    "embed_chunks": lambda *args: {}, "index_chunks": index}
                    for name, action in replacements.items():
                        stack.enter_context(patch.object(self.main, name, side_effect=action))
                    if failed_operation in {"chunk_document", "embed_chunks"}:
                        stack.enter_context(patch.object(self.main, failed_operation, side_effect=RuntimeError("sanitized processing failure")))
                    result = list(self.main._process_document("fixture.pdf", b"fixture", document_id=key))[-1]
                self.assertEqual(result["status"], "Failed")
                self.assertFalse(result["cleanup_pending"])
                for store in (state, chunks, blobs, slots):
                    self.assertFalse(store)

    def test_retry_worker_survives_a_pass_failure(self):
        import asyncio
        from unittest.mock import AsyncMock
        with patch.object(self.main, "run_in_threadpool", new_callable=AsyncMock) as run, \
                patch.object(self.main.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            run.side_effect = [RuntimeError("temporary outage"), None]
            sleep.side_effect = [None, asyncio.CancelledError()]
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self.main.cleanup_retry_worker())
            self.assertEqual(run.await_count, 2)
            run.assert_awaited_with(self.main.retry_pending_cleanups)
            sleep.assert_awaited_with(30)

    def test_failed_upload_cleanup_marker_is_minimal_hidden_and_retryable(self):
        record = self.record(status="Failed", failed_stage="Extraction", extraction_result={"private": "text"}, answer="private")
        state, chunks, blobs, slots, _ = self.coordinator([record], failure=("blobs", "disposable-one"))
        self.assertFalse(self.service.cleanup_failed_upload(record))
        marker = state["disposable-one"]
        self.assertEqual(marker["status"], "FAILED_CLEANUP")
        self.assertNotIn("extraction_result", marker)
        self.assertNotIn("answer", marker)
        with patch.object(self.service, "original_document_exists") as exists:
            self.assertEqual(self.service.list_documents(), [])
            exists.assert_not_called()
        from backend.upload_capacity import _counted_ids
        self.assertEqual(_counted_ids({"version": 2, "document_ids": ["disposable-one"]}, list(state.values())), set())
        with patch.object(self.service, "delete_document_blobs", side_effect=lambda record, others: blobs.discard(record["document_id"]) or {}):
            self.assertEqual(self.service.delete_document("disposable-one")["status"], "Deleted")
        for store in (state, chunks, blobs, slots):
            self.assertFalse(store)

    def test_search_outage_does_not_leave_original_blob_or_capacity(self):
        record = self.record(status="Failed", failed_stage="Indexing", error_reason="Indexing failed: the service rate limit was reached (HTTP 429).")
        state, chunks, blobs, slots, events = self.coordinator([record], failure=("chunks", "disposable-one"))
        self.assertFalse(self.service.cleanup_failed_upload(record))
        self.assertEqual(chunks, {"disposable-one"})
        self.assertFalse(blobs)
        self.assertFalse(slots)
        self.assertEqual([stage for stage, _ in events], ["chunks", "blobs", "capacity"])
        self.assertEqual(state["disposable-one"]["error_reason"], record["error_reason"])
        from backend.cleanup_journal import load_pending_cleanups
        self.assertEqual(len(load_pending_cleanups()), 1)
        with patch.object(self.service, "delete_document_chunks", side_effect=lambda key: chunks.discard(key)):
            self.service.retry_pending_cleanups()
        self.assertFalse(state)
        self.assertFalse(chunks)
        self.assertEqual(load_pending_cleanups(), [])

    def test_cosmos_outage_does_not_stop_blob_cleanup_and_outbox_hides_stale_record(self):
        record = self.record(status="Extracting", failed_stage="Extraction")
        state, chunks, blobs, slots, _ = self.coordinator([record])
        with patch.object(self.service, "save_document_result", side_effect=RuntimeError("unavailable")), \
                patch.object(self.service, "list_document_records", side_effect=RuntimeError("unavailable")), \
                patch.object(self.service, "delete_document_record", side_effect=RuntimeError("unavailable")):
            self.assertFalse(self.service.cleanup_failed_upload(record))
        self.assertFalse(blobs)
        self.assertFalse(chunks)
        self.assertFalse(slots)
        self.assertEqual(state["disposable-one"]["status"], "Extracting")
        self.assertEqual(self.service.list_documents(), [])
        from backend.upload_capacity import _counted_ids
        self.assertEqual(_counted_ids({"version": 2, "document_ids": ["disposable-one"]}, list(state.values())), set())
        # A subsequent worker pass recovers using disk intent even without a Cosmos marker.
        self.service.retry_pending_cleanups()
        self.assertFalse(state)
        from backend.cleanup_journal import load_pending_cleanups
        self.assertEqual(load_pending_cleanups(), [])

    def test_missing_local_outbox_still_uses_cosmos_marker_for_retry(self):
        record = self.record(status="Failed")
        state, chunks, blobs, slots, _ = self.coordinator([record], failure=("chunks", "disposable-one"))
        with patch.object(self.service, "save_pending_cleanup", side_effect=OSError("disk unavailable")):
            self.assertFalse(self.service.cleanup_failed_upload(record))
        self.assertEqual(state["disposable-one"]["cleanup_protocol"], "compensation-v1")
        self.assertFalse(blobs)
        with patch.object(self.service, "delete_document_chunks", side_effect=lambda key: chunks.discard(key)):
            self.service.retry_pending_cleanups()
        self.assertFalse(state)

    def test_blob_cleanup_continues_past_an_individual_artifact_failure(self):
        record = self.record(extracted_json_url="https://test.blob.core.windows.net/artifacts/disposable-one.json",
                             artifact_urls=["https://test.blob.core.windows.net/artifacts/disposable-one-extra.json"])
        client = MagicMock(url="https://test.blob.core.windows.net")
        client.__enter__.return_value = client
        original, artifact, extra = MagicMock(), MagicMock(), MagicMock()
        artifact.delete_blob.side_effect = RuntimeError("unavailable")
        client.get_blob_client.side_effect = [original, artifact, extra]
        with patch.object(self.blobs, "CONTAINER_NAME", "originals"), patch.object(self.blobs.BlobServiceClient, "from_connection_string", return_value=client):
            with self.assertRaises(RuntimeError):
                self.blobs.delete_document_blobs(record, [])
        original.delete_blob.assert_called_once()
        extra.delete_blob.assert_called_once()

    def test_blob_cleanup_without_cosmos_requires_id_owned_paths(self):
        client = MagicMock(url="https://test.blob.core.windows.net")
        client.__enter__.return_value = client
        with patch.object(self.blobs, "CONTAINER_NAME", "originals"), patch.object(self.blobs.BlobServiceClient, "from_connection_string", return_value=client):
            self.blobs.delete_document_blobs(self.record(), None)
            client.get_blob_client.assert_called_once()
            client.get_blob_client.reset_mock()
            with self.assertRaises(RuntimeError):
                self.blobs.delete_document_blobs(self.record(blob_url="https://test.blob.core.windows.net/originals/shared.pdf"), None)
            client.get_blob_client.assert_not_called()

    def test_invalid_unrelated_artifact_does_not_hide_a_shared_original(self):
        client = MagicMock(url="https://test.blob.core.windows.net")
        client.__enter__.return_value = client
        shared = self.record("other", blob_url=self.record()["blob_url"], extracted_json_url="https://foreign.invalid/invalid")
        with patch.object(self.blobs, "CONTAINER_NAME", "originals"), patch.object(self.blobs.BlobServiceClient, "from_connection_string", return_value=client):
            self.assertEqual(self.blobs.delete_document_blobs(self.record(), [shared])["original"], "shared_preserved")
            client.get_blob_client.assert_not_called()

    def test_view_never_runs_compensation(self):
        record = self.record(status="Failed")
        with patch.object(self.service, "get_document_record", return_value=record), \
                patch.object(self.service, "read_original_document", return_value=(b"pdf", "same.pdf")), \
                patch.object(self.service, "cleanup_failed_upload") as cleanup:
            self.assertEqual(self.service.view_document(record["document_id"]), (b"pdf", "same.pdf"))
            cleanup.assert_not_called()

    def test_outbox_strips_credentials_and_survives_module_reads(self):
        from backend.cleanup_journal import save_pending_cleanup, load_pending_cleanups, remove_pending_cleanup
        record = self.record(blob_url=self.record()["blob_url"] + "?sig=private-token",
                             artifact_urls=["https://test.blob.core.windows.net/artifacts/disposable-one.json?sig=private-token"])
        save_pending_cleanup(record)
        records = load_pending_cleanups()
        self.assertEqual(records[0]["document_id"], record["document_id"])
        self.assertNotIn("private-token", str(records))
        remove_pending_cleanup(record["document_id"])
        remove_pending_cleanup(record["document_id"])
        self.assertEqual(load_pending_cleanups(), [])

    def test_service_failure_reason_is_specific_without_sdk_body(self):
        from azure.core.exceptions import HttpResponseError
        error = HttpResponseError("private response with credentials")
        for status, expected in [(400, "rejected"), (403, "access was denied"), (429, "rate limit"), (503, "temporarily unavailable")]:
            error.status_code = status
            message = self.main._pipeline_failure_reason("Document extraction", error)
            self.assertIn(expected, message)
            self.assertIn(str(status), message)
            self.assertNotIn("private", message)
        error.error = SimpleNamespace(code="InvalidContent")
        message = self.main._pipeline_failure_reason("Document extraction", error)
        self.assertIn("InvalidContent", message)
        self.assertNotIn("private", message)

    def test_failure_before_resources_does_not_create_record(self):
        self.coordinator([])
        with patch.object(self.service, "save_document_result") as save:
            self.assertTrue(self.service.cleanup_failed_upload(self.record(), resources_possible=False))
            save.assert_not_called()

    def test_pending_failures_excluded_but_processing_stays_visible(self):
        self.coordinator([self.record(status="Failed"), self.record("pending", status="PENDING_CLEANUP"),
                          self.record("processing", status="Extracting")])
        with patch.object(self.service, "original_document_exists", return_value=True):
            self.assertEqual([item["document_id"] for item in self.service.list_documents()], ["processing"])

    def test_batch_request_requires_explicit_scope(self):
        from pydantic import ValidationError
        for body in ({}, {"document_ids": []}, {"document_ids": [" "]},
                     {"all_documents": True, "document_ids": ["one"]},
                     {"document_ids": ["__upload_capacity__"]}):
            with self.subTest(body=body), self.assertRaises(ValidationError):
                self.main.DeleteDocumentsRequest(**body)
        self.assertTrue(self.main.DeleteDocumentsRequest(all_documents=True).all_documents)

    def test_batch_route_uses_snapshot_or_ids(self):
        with patch.object(self.main, "delete_documents", return_value={"results": []}) as delete:
            self.main.remove_documents(self.main.DeleteDocumentsRequest(all_documents=True))
            delete.assert_called_with(None)
            self.main.remove_documents(self.main.DeleteDocumentsRequest(document_ids=["one"]))
            delete.assert_called_with(["one"])

    def test_blob_original_and_artifact_cleanup_and_retry(self):
        record = self.record(extracted_json_url="https://test.blob.core.windows.net/artifacts/disposable-one.json",
                             artifact_urls=["https://test.blob.core.windows.net/artifacts/disposable-one-extra.json"])
        client = MagicMock()
        client.url = "https://test.blob.core.windows.net"
        client.__enter__.return_value = client
        with patch.object(self.blobs, "CONTAINER_NAME", "originals"), patch.object(self.blobs.BlobServiceClient, "from_connection_string", return_value=client):
            cleanup = self.blobs.delete_document_blobs(record, [])
            self.assertEqual(set(cleanup.values()), {"deleted"})
            self.assertEqual(client.get_blob_client.call_count, 3)
            client.get_blob_client.return_value.delete_blob.side_effect = ResourceNotFoundError()
            self.assertEqual(set(self.blobs.delete_document_blobs(record, []).values()), {"already_missing"})
        client.delete_container.assert_not_called()

    def test_shared_blob_is_preserved_and_foreign_reference_rejected(self):
        client = MagicMock(url="https://test.blob.core.windows.net")
        client.__enter__.return_value = client
        with patch.object(self.blobs, "CONTAINER_NAME", "originals"), patch.object(self.blobs.BlobServiceClient, "from_connection_string", return_value=client):
            shared = self.record("disposable-two", blob_url=self.record()["blob_url"])
            self.assertEqual(self.blobs.delete_document_blobs(self.record(), [shared])["original"], "shared_preserved")
            client.get_blob_client.assert_not_called()
            with self.assertRaises(ValueError):
                self.blobs.delete_document_blobs(self.record(extracted_json_url="https://foreign/artifacts/x"), [])
            client.get_blob_client.assert_not_called()

    def test_search_checks_every_result_and_uses_document_id(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.search.return_value = [{"chunk_id": "one-chunk"}, {"chunk_id": "two-chunk"}]
        client.delete_documents.return_value = [SimpleNamespace(key="one-chunk", succeeded=True), SimpleNamespace(key="two-chunk", succeeded=False)]
        with patch.object(self.search, "_configuration", return_value=("https://test", "key", "index")), patch.object(self.search, "SearchClient", return_value=client):
            with self.assertRaises(RuntimeError):
                self.search.delete_document_chunks("one'quoted")
            self.assertEqual(client.search.call_args.kwargs["filter"], "document_id eq 'one''quoted'")
            client.delete_documents.return_value[1].succeeded = True
            self.assertEqual(self.search.delete_document_chunks("one'quoted"), 2)


if __name__ == "__main__":
    unittest.main()
