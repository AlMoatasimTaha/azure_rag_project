"""Offline upload limits, batch orchestration, and concurrent admission tests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import importlib
import json
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import UploadFile, HTTPException
from pypdf import PdfWriter
from azure.cosmos.exceptions import (
    CosmosResourceNotFoundError, CosmosResourceExistsError, CosmosAccessConditionFailedError,
)
from backend.validator import validate_document, MAX_FILE_SIZE_BYTES


def pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


class FakeContainer:
    def __init__(self):
        self.state = None
        self.version = 0
        self.lock = Lock()

    def read_item(self, **kwargs):
        with self.lock:
            if self.state is None:
                raise CosmosResourceNotFoundError()
            return {**self.state, "document_ids": list(self.state["document_ids"])}

    def create_item(self, body):
        with self.lock:
            if self.state is not None:
                raise CosmosResourceExistsError()
            self.version += 1
            self.state = {**body, "_etag": str(self.version)}

    def replace_item(self, *, body, etag, **kwargs):
        with self.lock:
            if etag != self.state["_etag"]:
                raise CosmosAccessConditionFailedError()
            self.version += 1
            self.state = {**body, "_etag": str(self.version)}


class UploadTests(unittest.TestCase):
    def setUp(self):
        # Failure orchestration is covered with in-memory resource stores in
        # test_deletion; never let upload fixtures reach real Azure cleanup.
        self.cleanup_mock = patch.object(self.main, "cleanup_failed_upload", return_value=True)
        self.cleanup_mock.start()
        self.addCleanup(self.cleanup_mock.stop)
    @classmethod
    def setUpClass(cls):
        with patch("azure.cosmos.CosmosClient"):
            cls.main = importlib.import_module("backend.main")
            cls.capacity = importlib.import_module("backend.upload_capacity")

    def test_pdf_boundaries_and_invalid_files(self):
        for pages in (1, 2):
            self.assertEqual(validate_document("valid.pdf", pdf(pages)), (True, None))
        self.assertIn("contains 3 pages", validate_document("three.pdf", pdf(3))[1])
        self.assertIn("10 MB", validate_document("large.pdf", b"x" * (MAX_FILE_SIZE_BYTES + 1))[1])
        self.assertFalse(validate_document("empty.pdf", b"")[0])
        self.assertFalse(validate_document("bad.pdf", b"not a pdf")[0])
        self.assertFalse(validate_document("bad.exe", pdf())[0])
        self.assertFalse(validate_document("bad.docx", b"not a zip")[0])
        from docx import Document
        stream = BytesIO()
        Document().save(stream)
        self.assertIn("cannot be reliably verified", validate_document("valid.docx", stream.getvalue())[1])
        # Exactly 10 MiB is accepted; a single additional byte is not.
        content = pdf()
        self.assertTrue(validate_document("boundary.pdf", content + b" " * (MAX_FILE_SIZE_BYTES - len(content)))[0])

    def invoke(self, contents):
        uploads = [UploadFile(filename=name, file=BytesIO(data)) for name, data in contents]
        return asyncio.run(self.main.upload_document(files=uploads, file=None))

    def test_single_and_multiple_reuse_pipeline_and_unique_ids(self):
        def process(name, content, *, document_id):
            yield {"status": "Ready", "document_id": document_id, "file_name": name}
        for size in (1, 3):
            with patch.object(self.main, "reserve_document_slots") as reserve, patch.object(self.main, "_process_document", side_effect=process) as pipeline:
                result = self.invoke([("same.pdf", pdf())] * size)
            self.assertEqual([r["status"] for r in result["results"]], ["Ready"] * size)
            self.assertEqual(pipeline.call_count, size)
            self.assertEqual(len({r["document_id"] for r in result["results"]}), size)
            self.assertEqual(reserve.call_args.args[1], size)

    def test_invalid_files_never_reach_pipeline(self):
        with patch.object(self.main, "reserve_document_slots") as reserve, patch.object(self.main, "_process_document") as pipeline:
            results = self.invoke([("large.pdf", b"x" * (MAX_FILE_SIZE_BYTES + 1)), ("three.pdf", pdf(3))])["results"]
        self.assertEqual([r["status"] for r in results], ["Invalid", "Invalid"])
        pipeline.assert_not_called()
        self.assertEqual(reserve.call_args.args[0], [])

    def test_mixed_batch_returns_every_file_and_keeps_valid_subset(self):
        def process(name, content, *, document_id):
            yield {"status": "Ready"}
        with patch.object(self.main, "reserve_document_slots") as reserve, patch.object(self.main, "_process_document", side_effect=process) as pipeline:
            result = self.invoke([("good.pdf", pdf()), ("three.pdf", pdf(3)), ("also.pdf", pdf(2))])
        self.assertEqual([r["status"] for r in result["results"]], ["Ready", "Invalid", "Ready"])
        self.assertEqual(pipeline.call_count, 2)
        self.assertEqual(len(reserve.call_args.args[0]), 2)

    def test_over_twenty_files_rejected_before_pipeline(self):
        with patch.object(self.main, "_process_document") as pipeline:
            with self.assertRaises(HTTPException) as error:
                self.invoke([("many.pdf", pdf())] * 21)
        self.assertEqual(error.exception.status_code, 422)
        self.assertIn("maximum of 20", error.exception.detail)
        pipeline.assert_not_called()

    def test_remaining_capacity_rejects_whole_batch(self):
        fake = FakeContainer()
        existing = [{"document_id": str(i), "file_sha256": f"existing-{i}"} for i in range(18)]
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=existing), patch.object(self.main, "_process_document") as pipeline:
            with self.assertRaises(HTTPException) as error:
                self.invoke([("new.pdf", pdf() + f"\n% different-{i}".encode()) for i in range(3)])
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("only 2 more", error.exception.detail)
        self.assertIsNone(fake.state)
        pipeline.assert_not_called()

    def test_concurrent_batches_cannot_exceed_twenty(self):
        fake = FakeContainer()
        existing = [{"document_id": str(i), "file_sha256": f"existing-{i}"} for i in range(18)]
        def attempt(i):
            try:
                self.capacity.reserve_document_slots([f"new-{i}"], 1)
                return True
            except self.capacity.CapacityError:
                return False
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=existing):
            with ThreadPoolExecutor(max_workers=8) as executor:
                accepted = list(executor.map(attempt, range(8)))
            self.assertEqual(sum(accepted), 2)
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 20)
            allocated = next(value for value in fake.state["document_ids"] if value.startswith("new-"))
            self.capacity.release_document_slot(allocated)
            self.assertEqual(self.capacity.get_upload_capacity()["remaining_slots"], 1)

    def test_failed_file_does_not_stop_later_files_or_expose_exception(self):
        def process(name, content, *, document_id):
            if name == "failed.pdf":
                raise RuntimeError("secret error")
            yield {"status": "Ready"}
        with patch.object(self.main, "reserve_document_slots"), patch.object(self.main, "save_document_result"), patch.object(self.main, "_process_document", side_effect=process):
            result = self.invoke([("failed.pdf", pdf()), ("good.pdf", pdf())])
        self.assertEqual([r["status"] for r in result["results"]], ["Failed", "Ready"])
        self.assertNotIn("secret error", str(result))

    def test_pipeline_passes_id_to_blob_and_keeps_rag_stages(self):
        with patch.object(self.main, "save_document_result"), patch.object(self.main, "upload_to_blob", return_value="https://example/documents/id/a.pdf") as upload, patch.object(self.main, "extract_document", return_value={}), patch.object(self.main, "chunk_document", return_value={"text_chunks": [{}], "table_chunks": [], "chunking": {"text_chunk_size": 1, "text_overlap": 0}}), patch.object(self.main, "embed_chunks", return_value={}), patch.object(self.main, "index_chunks", return_value=[SimpleNamespace(succeeded=True)]):
            updates = list(self.main._process_document("a.pdf", pdf(), document_id="fixed-id"))
        self.assertEqual(updates[-1]["status"], "Ready")
        self.assertEqual(updates[-1]["document_id"], "fixed-id")
        self.assertEqual(upload.call_args.kwargs["document_id"], "fixed-id")
        self.assertEqual([item["status"] for item in updates],
                         ["Valid", "Uploading", "Extracting", "Chunking", "Embedding", "Indexing", "Ready"])

    def test_stream_reports_each_stage_and_keeps_json_summary(self):
        def process(name, content, *, document_id):
            for status in ["Valid", "Uploading", "Extracting", "Chunking", "Embedding", "Indexing", "Ready"]:
                yield {"status": status}

        async def collect():
            upload = UploadFile(filename="stages.pdf", file=BytesIO(pdf()))
            response = await self.main.upload_document(files=[upload], file=None, progress=True)
            return [json.loads(line) async for line in response.body_iterator]

        with patch.object(self.main, "reserve_document_slots"), patch.object(self.main, "_process_document", side_effect=process):
            events = asyncio.run(collect())
        self.assertEqual([event["status"] for event in events[:-1]],
                         ["Validating", "Valid", "Uploading", "Extracting", "Chunking", "Embedding", "Indexing", "Ready"])
        self.assertEqual(events[-1]["type"], "complete")
        self.assertEqual(events[-1]["results"][0]["status"], "Ready")
        self.assertTrue(all(event["file_name"] == "stages.pdf" for event in events[:-1]))

    def test_disconnecting_progress_does_not_interrupt_processing(self):
        from threading import Event
        started, finish = Event(), Event()
        def process(name, content, *, document_id):
            started.set()
            yield {"status": "Extracting"}
            finish.wait(timeout=5)
            yield {"status": "Ready"}

        async def disconnect():
            upload = UploadFile(filename="disconnect.pdf", file=BytesIO(pdf()))
            response = await self.main.upload_document(files=[upload], file=None, progress=True)
            await anext(response.body_iterator)
            self.assertTrue(started.is_set())
            tasks = list(self.main._upload_tasks)
            await response.body_iterator.aclose()
            finish.set()
            result = await asyncio.gather(*tasks)
            self.assertEqual(result[0]["results"][0]["status"], "Ready")

        with patch.object(self.main, "reserve_document_slots"), patch.object(self.main, "_process_document", side_effect=process):
            asyncio.run(disconnect())

    def test_root_blob_names_do_not_overwrite_and_keep_display_name(self):
        from backend import blob_service
        with patch.object(blob_service, "BlobServiceClient") as factory:
            client = factory.from_connection_string.return_value
            blob_service.upload_to_blob("path/same.pdf", b"first", document_id="one")
            blob_service.upload_to_blob("same.pdf", b"second", document_id="two")
            blobs = client.get_container_client.return_value
            self.assertEqual([call.args[0] for call in blobs.get_blob_client.call_args_list], ["one_same.pdf", "two_same.pdf"])
            blobs.get_blob_client.return_value.upload_blob.assert_called_with(b"second", overwrite=False)

    def test_identical_content_in_batch_and_renamed_file_are_duplicates(self):
        from hashlib import sha256
        digest = sha256(pdf()).hexdigest()
        fake = FakeContainer()
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=[]):
            duplicates = self.capacity.reserve_document_slots(["one", "two"], 2, {"one": digest, "two": digest})
            self.assertEqual(duplicates, {"two": "one"})
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 1)
            self.assertEqual(self.capacity.reserve_document_slots(["renamed"], 1, {"renamed": digest}), {"renamed": "one"})
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 1)
            # Deletion frees both the slot and the content fingerprint.
            self.capacity.release_document_slot("one")
            self.assertEqual(self.capacity.reserve_document_slots(["again"], 1, {"again": digest}), {})

    def test_different_content_with_same_name_still_allowed(self):
        fake = FakeContainer()
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=[]):
            self.assertEqual(self.capacity.reserve_document_slots(["one", "two"], 2, {"one": "hash-a", "two": "hash-b"}), {})
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 2)

    def test_concurrent_duplicate_requests_accept_only_one(self):
        fake = FakeContainer()
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=[]):
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda i: self.capacity.reserve_document_slots([f"doc-{i}"], 1, {f"doc-{i}": "same-hash"}), range(8)))
            self.assertEqual(sum(not result for result in results), 1)
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 1)

    def test_failed_cleanup_releases_admission_under_concurrency(self):
        fake = FakeContainer()
        records = [{"document_id": f"ready-{i}", "status": "Ready", "file_sha256": f"hash-{i}"} for i in range(19)]
        records.append({"document_id": "failed", "status": "FAILED_CLEANUP"})
        fake.create_item({"version": 2, "document_ids": [record["document_id"] for record in records]})
        def attempt(index):
            try:
                self.capacity.reserve_document_slots([f"new-{index}"], 1)
                return True
            except self.capacity.CapacityError:
                return False
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=records):
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(attempt, range(8)))
            self.assertEqual(sum(results), 1)
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 20)
            self.assertNotIn("failed", fake.state["document_ids"])

    def test_deleting_retry_record_is_not_readmitted_after_slot_release(self):
        fake = FakeContainer()
        records = [{"document_id": "deleting", "status": "Deleting"}]
        fake.create_item({"version": 2, "document_ids": ["deleting"]})
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=records):
            self.capacity.release_document_slot("deleting")
            self.capacity.reserve_document_slots(["new"], 1)
            self.assertEqual(fake.state["document_ids"], ["new"])

    def test_validation_failure_does_not_consume_full_system_capacity(self):
        fake = FakeContainer()
        records = [{"document_id": str(i), "status": "Ready"} for i in range(20)]
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=records):
            self.assertEqual(self.capacity.reserve_document_slots([], 1), {})
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 20)

    def test_legacy_original_is_hashed_without_overwriting_status(self):
        from hashlib import sha256
        from unittest.mock import MagicMock
        fake = FakeContainer()
        fake.patch_item = MagicMock()
        records = [{"id": "old", "document_id": "old", "status": "Ready", "file_name": "old.pdf"}]
        digest = sha256(b"original").hexdigest()
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=records), patch.object(self.capacity, "read_original_document", return_value=(b"original", "old.pdf")):
            self.assertEqual(self.capacity.reserve_document_slots(["new"], 1, {"new": digest}), {"new": "old"})
        fake.patch_item.assert_called_once_with(item="old", partition_key="old", patch_operations=[{"op": "add", "path": "/file_sha256", "value": digest}])

    def test_duplicate_endpoint_result_never_enters_pipeline(self):
        fake = FakeContainer()
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=[]), patch.object(self.main, "_process_document", return_value=iter([{"status": "Ready"}])) as pipeline:
            result = self.invoke([("one.pdf", pdf()), ("renamed.pdf", pdf())])
        self.assertEqual([row["status"] for row in result["results"]], ["Ready", "Invalid"])
        self.assertTrue(result["results"][1]["duplicate"])
        self.assertIn("already uploaded", result["results"][1]["message"])
        self.assertEqual(pipeline.call_count, 1)

    def test_inactive_records_and_old_deleted_reservations_do_not_count(self):
        fake = FakeContainer()
        fake.create_item({"id": "control", "document_ids": ["old-deleted", "failed"]})
        records = [{"document_id": "ready", "status": "Ready"},
                   {"document_id": "processing", "status": "Embedding"},
                   *[{"document_id": status.lower(), "status": status} for status in ["Failed", "Invalid", "Deleted"]]]
        with patch.object(self.capacity, "container", fake), patch.object(self.capacity, "list_document_records", return_value=records):
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 2)
            self.capacity.reserve_document_slots(["new"], 1)
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 3)
            self.capacity.release_document_slot("new")
            self.assertEqual(self.capacity.get_upload_capacity()["current_count"], 2)

    def test_delete_cleans_by_id_then_releases_capacity_and_keeps_retry_metadata(self):
        from backend import document_service
        save = patch.object(document_service, "save_document_result")
        save.start()
        self.addCleanup(save.stop)
        record = {"id": "one", "document_id": "one", "file_name": "same.pdf", "status": "Ready"}
        with patch.object(document_service, "get_document_record", return_value=record), patch.object(document_service, "list_document_records", return_value=[record]), patch.object(document_service, "delete_document_chunks", return_value=4) as search, patch.object(document_service, "delete_document_blobs", return_value={"original": "deleted"}), patch.object(document_service, "delete_document_record") as delete, patch.object(document_service, "release_document_slot") as release:
            result = document_service.delete_document("one")
            search.assert_called_once_with("one")
            delete.assert_called_once_with({**record, "status": "Deleting"})
            release.assert_called_once_with("one")
            self.assertEqual(result["deleted_chunk_count"], 4)
        with patch.object(document_service, "get_document_record", return_value=record), patch.object(document_service, "list_document_records", return_value=[]), patch.object(document_service, "delete_document_chunks", return_value=4), patch.object(document_service, "delete_document_blobs", side_effect=RuntimeError("offline")), patch.object(document_service, "delete_document_record") as delete, patch.object(document_service, "release_document_slot") as release:
            with self.assertRaises(RuntimeError):
                document_service.delete_document("one")
            delete.assert_not_called()
            release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
