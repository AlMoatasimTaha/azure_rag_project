"""Production chunking checks. External services are mocked, never called."""

from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.chunking_service import (
    _index_boundaries, _span_range, _split_table, chunk_document, count_tokens,
)


def document_fixture(items, index_type="unicodeCodePoint"):
    text, paragraphs, pages = "", [], {}
    for value, role, number, x, y, size in items:
        offset = len(_index_boundaries(text, index_type)) - 1
        length = len(_index_boundaries(value, index_type)) - 1
        paragraphs.append({"content": value, "role": role,
            "spans": [{"offset": offset, "length": length}], "boundingRegions": [
                {"pageNumber": number, "polygon": [x, y, x + 2, y, x + 2, y + size, x, y + size]}]})
        page = pages.setdefault(number, {"pageNumber": number, "width": 8.5, "height": 11, "spans": []})
        page["spans"].append({"offset": offset, "length": len(_index_boundaries(value + "\n", index_type)) - 1})
        text += value + "\n"
    return {"document_id": "test-doc", "file_name": "generic.docx", "blob_url": "https://original/document.docx",
            "extraction_result": {"content": text, "stringIndexType": index_type,
                                  "paragraphs": paragraphs, "pages": list(pages.values()), "tables": []}}


def canonical(output):
    result = deepcopy(output)
    ids = {c["chunk_id"]: f"{group}_{index}" for group in ("text_chunks", "table_chunks")
           for index, c in enumerate(result[group])}
    for group in ("text_chunks", "table_chunks"):
        for chunk in result[group]:
            chunk["chunk_id"] = ids[chunk["chunk_id"]]
            for field in ("related_text_chunk_ids", "related_table_ids"):
                if field in chunk:
                    chunk[field] = [ids[key] for key in chunk[field]]
    return result


class ChunkingTests(unittest.TestCase):
    def test_stacked_headings_and_paragraphs_stay_together(self):
        source = document_fixture([
            ("PART 17", "sectionHeading", 1, 1, 1, .13),
            ("Arbitrary subject", "sectionHeading", 1, 1, 1.2, .2),
            ("First paragraph.", None, 1, 1, 1.5, .15),
            ("Second paragraph.", None, 1, 1, 2, .15),
            ("Another subject", "sectionHeading", 1, 1, 3, .2),
            ("Other content.", None, 1, 1, 3.3, .15)])
        original = deepcopy(source)
        output = chunk_document(source)
        self.assertEqual(source, original)
        self.assertEqual(len(output["text_chunks"]), 2)
        first = output["text_chunks"][0]
        self.assertEqual(first["section_heading"], "PART 17 Arbitrary subject")
        self.assertIn("First paragraph.", first["content"])
        self.assertIn("Second paragraph.", first["content"])

    def test_large_sections_split_at_paragraph_boundaries(self):
        paragraphs = [f"Paragraph {i}: " + "content " * 120 for i in range(8)]
        source = document_fixture([("New subject", "sectionHeading", 1, 1, 1, .2)] +
            [(value, None, 1, 1, 2 + i, .15) for i, value in enumerate(paragraphs)])
        chunks = chunk_document(source)["text_chunks"]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c["section_heading"] == "New subject" for c in chunks))
        for value in paragraphs:
            self.assertEqual(sum(value.rstrip() in c["content"] for c in chunks), 1)

    def test_exact_limit_and_unicode_span_units(self):
        for index_type in ("unicodeCodePoint", "utf16CodeUnit", "textElements"):
            for text in ("word " * 399 + "word", "word " * 400 + "word", "Arabic العربية 😀 e\u0301 " * 160):
                with self.subTest(index_type=index_type, tokens=count_tokens(text)):
                    source = document_fixture([(text, None, 1, 1, 1, .15)], index_type)
                    chunks = chunk_document(source)["text_chunks"]
                    self.assertTrue(all(c["token_count"] <= 400 for c in chunks))
                    if count_tokens(text) == 400:
                        self.assertEqual(len(chunks), 1)
                    boundaries = _index_boundaries(source["extraction_result"]["content"], index_type)
                    restored = "".join(source["extraction_result"]["content"][slice(*_span_range(s, boundaries))]
                                       for c in chunks for s in c["spans"])
                    self.assertEqual("".join(restored.split()), "".join(text.split()))

    def test_ids_are_unique_across_documents(self):
        source = document_fixture([("Some content.", None, 1, 1, 1, .15)])
        first = chunk_document(source)["text_chunks"][0]["chunk_id"]
        second = chunk_document(source)["text_chunks"][0]["chunk_id"]
        self.assertNotEqual(first, second)

    def test_repeated_heading_names_do_not_link_different_sections(self):
        source = document_fixture([("Same heading", "sectionHeading", 1, 1, 1, .2),
            ("First section.", None, 1, 1, 1.4, .15),
            ("Same heading", "sectionHeading", 1, 1, 3, .2),
            ("Second section.", None, 1, 1, 3.4, .15),
            ("Table value", None, 1, 1, 4, .15)])
        paragraph = source["extraction_result"]["paragraphs"][-1]
        source["extraction_result"]["tables"] = [{"spans": paragraph["spans"], "rowCount": 1, "columnCount": 1,
            "boundingRegions": paragraph["boundingRegions"], "cells": [{"rowIndex": 0, "columnIndex": 0,
                "content": "Table value", "spans": paragraph["spans"], "boundingRegions": paragraph["boundingRegions"]}]}]
        output = chunk_document(source)
        table = output["table_chunks"][0]
        self.assertEqual(table["related_text_chunk_ids"], [output["text_chunks"][1]["chunk_id"]])
        self.assertEqual(output["text_chunks"][0]["related_table_ids"], [])

    def test_invalid_source_and_uncovered_text_raise(self):
        with self.assertRaises(ValueError):
            chunk_document({})
        source = document_fixture([("Uncovered text.", None, 1, 1, 1, .15)])
        source["extraction_result"]["paragraphs"] = []
        with self.assertRaisesRegex(ValueError, "uncovered"):
            chunk_document(source)
        source = document_fixture([("Some text.", None, 1, 1, 1, .15)])
        source["extraction_result"]["pages"][0]["spans"].append({"offset": 1, "length": 2})
        with self.assertRaisesRegex(ValueError, "overlap"):
            chunk_document(source)

    def test_table_fragments_preserve_unicode_and_column_labels(self):
        for index_type in ("unicodeCodePoint", "utf16CodeUnit", "textElements"):
            with self.subTest(index_type=index_type):
                values = ["ID", "Details", "CASE-17", "Arabic العربية 😀 e\u0301 " * 180]
                source = document_fixture([(v, None, 1, 1, i + 1, .15) for i, v in enumerate(values)], index_type)
                cells = [{"rowIndex": i // 2, "columnIndex": i % 2, "content": value,
                          "spans": source["extraction_result"]["paragraphs"][i]["spans"]}
                         for i, value in enumerate(values)]
                for cell in cells[:2]:
                    cell["kind"] = "columnHeader"
                parts = _split_table({"cells": cells}, source["extraction_result"])
                fragments = [c for p in parts for c in p["cells"] if c["rowIndex"] == 1 and c["columnIndex"] == 1]
                self.assertEqual("".join(c["content"] for c in fragments), values[-1])
                self.assertTrue(all(count_tokens(p["content"]) <= 400 for p in parts))
                self.assertTrue(all("CASE-17" in p["content"] and "Details:" in p["content"] for p in parts))

    def test_existing_embedding_and_search_preparation_accept_chunks(self):
        from backend.embedding_service import embed_chunks
        from backend.search_service import _prepare_documents
        source = document_fixture([("Some content.", None, 1, 1, 1, .15)])
        chunked = chunk_document(source)
        with patch("backend.embedding_service.generate_embedding", return_value=[.1] * 1536) as embed:
            embedded = embed_chunks(chunked)
        documents, dimension = _prepare_documents(embedded)
        self.assertEqual(dimension, 1536)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["content"], "Some content.")
        embed.assert_called_once_with("Some content.")
        self.assertNotIn("content_vector", chunked["text_chunks"][0])

    def test_existing_upload_pipeline_uses_new_chunker(self):
        import backend.main as pipeline
        from backend.embedding_service import embed_chunks
        source = document_fixture([("PART 17", "sectionHeading", 1, 1, 1, .13),
            ("A subject", "sectionHeading", 1, 1, 1.2, .2), ("Some content.", None, 1, 1, 1.6, .15)])
        def extract(name, data, **kwargs):
            result = deepcopy(source)
            result["document_id"], result["blob_url"] = kwargs["document_id"], kwargs["blob_url"]
            return result
        def index(embedded):
            return [SimpleNamespace(succeeded=True) for group in ("text_chunks", "table_chunks") for c in embedded[group]]
        with ExitStack() as stack:
            stack.enter_context(patch.object(pipeline, "validate_document", return_value=(True, None)))
            stack.enter_context(patch.object(pipeline, "save_document_result"))
            stack.enter_context(patch.object(pipeline, "upload_to_blob", return_value=source["blob_url"]))
            stack.enter_context(patch.object(pipeline, "extract_document", side_effect=extract))
            stack.enter_context(patch("backend.embedding_service.generate_embedding", return_value=[.1] * 1536))
            stack.enter_context(patch.object(pipeline, "embed_chunks", side_effect=embed_chunks))
            index_mock = stack.enter_context(patch.object(pipeline, "index_chunks", side_effect=index))
            events = list(pipeline._process_document("generic.docx", b"test"))
        self.assertEqual(events[-1]["status"], "Ready")
        self.assertEqual(events[-1]["text_chunk_count"], 1)
        self.assertEqual(events[-1]["chunk_overlap"], 0)
        self.assertEqual(index_mock.call_args.args[0]["text_chunks"][0]["section_heading"], "PART 17 A subject")

    def test_cached_sample_outputs_match_reviewed_experiment(self):
        folder = Path(__file__).resolve().parents[1] / "experiments" / "heading_chunking" / "output"
        path = folder / "extraction_structure_test.json"
        if not path.exists():
            self.skipTest("Optional cached sample extractions are not available")
        sources = json.loads(path.read_text(encoding="utf-8"))["documents"]
        for source in sources:
            expected_path = folder / f"{Path(source['file_name']).stem}.chunks_400.json"
            if not expected_path.exists():
                continue
            with self.subTest(document=source["file_name"]):
                expected = json.loads(expected_path.read_text(encoding="utf-8"))
                original = deepcopy(source)
                actual = chunk_document(source)
                self.assertEqual(source, original)
                self.assertEqual(canonical(actual), canonical(expected))
                for chunk in actual["text_chunks"] + actual["table_chunks"]:
                    self.assertLessEqual(chunk["token_count"], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
