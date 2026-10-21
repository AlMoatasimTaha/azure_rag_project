"""Standalone chunking for the JSON envelope written by extraction_service.

Offsets in output spans use Azure's original stringIndexType. Bounding polygons
describe source elements, not an estimated crop around individual tokens.
"""

from bisect import bisect_left
from copy import deepcopy
from functools import lru_cache
import json
import os
from pathlib import PurePosixPath
from uuid import uuid4

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv
import regex
import tiktoken


@lru_cache(maxsize=1)
def _encoding():
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count ordinary text tokens, including literal special-token strings."""
    return len(_encoding().encode(text, disallowed_special=()))


def _text_windows(text, chunk_size, overlap):
    if not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    if not isinstance(overlap, int) or not 0 <= overlap < chunk_size:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk_size")
    if not text:
        return

    encoding = _encoding()
    tokens = encoding.encode(text, disallowed_special=())
    # Match token byte offsets to complete text elements so Unicode is lossless.
    byte_to_char = {0: 0}
    byte_offset = 0
    for match in regex.finditer(r"\X", text):
        byte_offset += len(match.group().encode("utf-8"))
        byte_to_char[byte_offset] = match.end()
    token_bytes = [0]
    for token in tokens:
        token_bytes.append(token_bytes[-1] + len(encoding.decode_single_token_bytes(token)))
    boundaries = [i for i, offset in enumerate(token_bytes) if offset in byte_to_char]
    start = 0
    while start < len(tokens):
        position = bisect_left(boundaries, min(start + chunk_size, len(tokens)) + 1) - 1
        end = boundaries[position]
        while end > start:
            char_start = byte_to_char[token_bytes[start]]
            char_end = byte_to_char[token_bytes[end]]
            content = text[char_start:char_end]
            if count_tokens(content) <= chunk_size:
                break
            position -= 1
            end = boundaries[position]
        if end <= start:
            raise ValueError("A single text element exceeds chunk_size tokens")
        yield char_start, char_end, content
        if end == len(tokens):
            break
        # Usually end - overlap exactly; Unicode boundaries may reduce overlap.
        next_position = bisect_left(boundaries, max(start + 1, end - overlap))
        start = boundaries[next_position]


def chunk_text(text: str, chunk_size: int = 400, overlap: int = 80) -> list[str]:
    """Return token windows, retaining complete Unicode text elements."""
    return [content for _, _, content in _text_windows(text, chunk_size, overlap)]


def _index_boundaries(content, index_type):
    """Map Azure string indices to Python character offsets without guessing."""
    if index_type == "unicodeCodePoint":
        return list(range(len(content) + 1))
    if index_type == "textElements":
        return [0] + [match.end() for match in regex.finditer(r"\X", content)]
    if index_type == "utf16CodeUnit":
        offsets = [0]
        for index, character in enumerate(content):
            if ord(character) > 0xFFFF:
                offsets.append(None)  # The middle of a surrogate pair is invalid.
            offsets.append(index + 1)
        return offsets
    raise ValueError("extraction_result.stringIndexType must identify Azure's span units")


def _span_range(span, boundaries):
    start = span["offset"]
    if type(start) is not int or type(span["length"]) is not int or span["length"] < 0:
        raise ValueError("Span offset/length must be nonnegative integers")
    end = start + span["length"]
    if not 0 <= start <= end < len(boundaries):
        raise ValueError("An extraction span is outside extraction_result.content")
    if boundaries[start] is None or boundaries[end] is None:
        raise ValueError("An extraction span splits a Unicode surrogate pair")
    return boundaries[start], boundaries[end]


def _related_spans(element, chunk_spans):
    related = []
    for span in element.get("spans", []):
        for chunk_span in chunk_spans:
            start = max(span["offset"], chunk_span["offset"])
            end = min(span["offset"] + span["length"],
                      chunk_span["offset"] + chunk_span["length"])
            if start < end:
                related.append({"offset": start, "length": end - start})
    return related


def _regions(element, page_number):
    return deepcopy([region for region in element.get("boundingRegions", [])
                     if region.get("pageNumber") == page_number])


def _merge_spans(spans):
    merged = []
    for span in sorted(spans, key=lambda item: item["offset"]):
        start, end = span["offset"], span["offset"] + span["length"]
        if merged and start <= merged[-1]["offset"] + merged[-1]["length"]:
            merged[-1]["length"] = max(end, merged[-1]["offset"] + merged[-1]["length"]) - merged[-1]["offset"]
        elif end > start:
            merged.append({"offset": start, "length": end - start})
    return merged


def _without_tables(page_spans, table_spans):
    """Subtract table intervals using the original Azure offset units."""
    remaining = []
    for span in page_spans:
        intervals = [(span["offset"], span["offset"] + span["length"])]
        for table_span in table_spans:
            start, end = table_span["offset"], table_span["offset"] + table_span["length"]
            pieces = []
            for left, right in intervals:
                if right <= start or left >= end:
                    pieces.append((left, right))
                else:
                    if left < start:
                        pieces.append((left, start))
                    if end < right:
                        pieces.append((end, right))
            intervals = pieces
        remaining.extend({"offset": left, "length": right - left} for left, right in intervals)
    return remaining


def _heading(paragraphs, page_number, offset):
    headings = [(min(span["offset"] for span in paragraph["spans"]), paragraph.get("content"))
                for paragraph in paragraphs
                if paragraph.get("role") in {"title", "sectionHeading"}
                and paragraph.get("spans") and _regions(paragraph, page_number)
                and min(span["offset"] for span in paragraph["spans"]) <= offset]
    return max(headings, key=lambda item: item[0])[1] if headings else None


def table_to_text(cells, header_cells=None):
    """Serialize rows with explicit column labels; retain merged/header cells as metadata.

    Unlabelled columns use their original column number. For page-spanning tables,
    headers may come from a preceding page without copying those cells into text.
    """
    headers = [cell for cell in (header_cells if header_cells is not None else cells)
               if cell.get("kind") == "columnHeader"]
    lines = []
    for row in sorted({cell["rowIndex"] for cell in cells}):
        values = []
        for cell in sorted((cell for cell in cells if cell["rowIndex"] == row
                            and cell.get("kind") != "columnHeader"),
                           key=lambda item: item["columnIndex"]):
            labels = [" ".join(header.get("content", "").split()) for header in headers
                      if header["rowIndex"] < row
                      and header["columnIndex"] < cell["columnIndex"] + cell.get("columnSpan", 1)
                      and cell["columnIndex"] < header["columnIndex"] + header.get("columnSpan", 1)]
            label = " / ".join(dict.fromkeys(labels)) or f"Column {cell['columnIndex'] + 1}"
            values.append(f"{label}: {' '.join(cell.get('content', '').split())}")
        if values:
            lines.append(" | ".join(values))
    # Header-only fragments still have searchable content.
    if not lines:
        lines = [" | ".join(" ".join(cell.get("content", "").split()) for cell in cells)]
    # A sparse table can have a header with no data below it. Keep that label
    # searchable too, without fabricating a value or changing the original cells.
    readable = "\n".join(lines)
    unused_headers = [" ".join(header.get("content", "").split()) for header in headers
                      if " ".join(header.get("content", "").split()) not in readable]
    if unused_headers:
        lines.insert(0, "Headers: " + " | ".join(dict.fromkeys(unused_headers)))
    return "\n".join(lines)


def _link_tables(text_chunks, table_chunks, paragraphs, excluded, pages):
    """Link a table to its nearest same-section source paragraph, not a whole page."""
    for table in table_chunks:
        for page_number in table["page_numbers"]:
            page = next(page for page in pages if page["pageNumber"] == page_number)
            local_spans = _related_spans(table, page["spans"])
            if not local_spans:
                continue
            offset = min(span["offset"] for span in local_spans)
            section = _heading(paragraphs, page_number, offset)
            candidates = [chunk for chunk in text_chunks if page_number in chunk["page_numbers"]]
            anchors = []
            for paragraph in paragraphs:
                spans = paragraph.get("spans", [])
                if (not spans or not _regions(paragraph, page_number)
                        or paragraph.get("role") in {"pageHeader", "pageFooter", "pageNumber", "title", "sectionHeading"}
                        or _related_spans(paragraph, excluded)):
                    continue
                start = min(span["offset"] for span in spans)
                if _heading(paragraphs, page_number, start) != section:
                    continue
                end = max(span["offset"] + span["length"] for span in spans)
                anchors.append((0 if end <= offset else 1, abs(offset - end), paragraph))
            related = []
            if anchors:
                anchor = min(anchors, key=lambda item: item[:2])[2]
                related = [chunk for chunk in candidates if _related_spans(anchor, chunk["spans"])]
            if not related:
                # Use source positions rather than a chunk's opening heading: a
                # 400-token window can contain text from more than one section.
                nearby = []
                for chunk in candidates:
                    for span in chunk["spans"]:
                        position = min(max(offset, span["offset"]), span["offset"] + span["length"] - 1)
                        if _heading(paragraphs, page_number, position) == section:
                            nearby.append((abs(offset - position), chunk))
                if nearby:
                    related = [min(nearby, key=lambda item: item[0])[1]]
            for chunk in related:
                if table["chunk_id"] not in chunk["related_table_ids"]:
                    chunk["related_table_ids"].append(table["chunk_id"])
                    table["related_text_chunk_ids"].append(chunk["chunk_id"])


def chunk_document(extracted_json: dict) -> dict:
    """Return separate text/table chunks without mutating the extraction envelope.

    Text windows are built after table-span subtraction, at 400/80 tokens per
    page. One Azure table object becomes one logical chunk, including all its
    pages. Separate Azure table objects are never heuristically merged. Tables
    are not token-split. Source span units and original cell metadata are retained.
    """
    result = extracted_json.get("extraction_result")
    if not isinstance(result, dict):
        raise ValueError("Expected the saved JSON envelope with extraction_result")
    content = result.get("content")
    pages = result.get("pages")
    if not isinstance(content, str) or not isinstance(pages, list):
        raise ValueError("extraction_result must contain content and pages")
    index_type = result.get("stringIndexType")
    boundaries = _index_boundaries(content, index_type)
    char_to_index = {char: index for index, char in enumerate(boundaries) if char is not None}
    paragraphs = result.get("paragraphs", [])
    tables = result.get("tables", [])
    table_ranges = []
    for table in tables:
        spans = table.get("spans", []) + [span for cell in table.get("cells", [])
                                          for span in cell.get("spans", [])]
        if not spans or not table.get("cells"):
            raise ValueError("Each table needs cells and source spans to remove it safely from text")
        for span in spans:
            _span_range(span, boundaries)
        table_ranges.append(_merge_spans(spans))
        if not table_ranges[-1]:
            raise ValueError("Each table needs at least one nonempty source span")
    excluded = _merge_spans([span for spans in table_ranges for span in spans])
    text_chunks, table_chunks = [], []
    common = {
        "document_id": extracted_json.get("document_id"),
        "file_name": extracted_json.get("file_name"),
        "source_blob_url": extracted_json.get("blob_url"),
        "extracted_json_url": extracted_json.get("extracted_json_url"),
        "string_index_type": index_type,
    }
    seen_pages = set()
    for page in pages:
        page_number = page.get("pageNumber")
        if type(page_number) is not int or page_number < 1 or page_number in seen_pages:
            raise ValueError("Each page must have a unique positive pageNumber")
        seen_pages.add(page_number)
        if "spans" not in page:
            raise ValueError(f"Page {page_number} is missing spans")
        page_spans = sorted(page["spans"], key=lambda item: item["offset"])
        previous_end = -1
        for span in page_spans:
            _span_range(span, boundaries)
            if span["offset"] < previous_end:
                raise ValueError("Page spans must not overlap")
            previous_end = span["offset"] + span["length"]
        segments = []
        local_offset = 0
        for span in _without_tables(page_spans, excluded):
            start, end = _span_range(span, boundaries)
            if segments and start < segments[-1][3]:
                raise ValueError("Page spans must not overlap")
            segments.append((local_offset, local_offset + end - start, start, end))
            local_offset += end - start
        page_text = "".join(content[start:end] for _, _, start, end in segments)
        for local_start, local_end, text in _text_windows(page_text, 400, 80):
            if not text.strip():
                continue
            spans = []
            for segment_start, segment_end, source_start, _ in segments:
                start = max(local_start, segment_start)
                end = min(local_end, segment_end)
                if start < end:
                    source_offset = char_to_index[source_start + start - segment_start]
                    source_end = char_to_index[source_start + end - segment_start]
                    spans.append({"offset": source_offset, "length": source_end - source_offset})
            chunk = {
                **common,
                "chunk_id": f"text_{uuid4()}",
                "chunk_type": "text",
                "chunk_index": len(text_chunks),
                "page_number": page_number,
                "page_numbers": [page_number],
                "content": text,
                "token_count": count_tokens(text),
                "spans": spans,
                "bounding_regions": [],
                "section_heading": _heading(paragraphs, page_number, spans[0]["offset"]),
                "related_table_ids": [],
            }
            for paragraph in paragraphs:
                regions = _regions(paragraph, page_number)
                if not regions:
                    continue
                if _related_spans(paragraph, spans):
                    chunk["bounding_regions"].extend(regions)
            text_chunks.append(chunk)
    # Reject ambiguous page mappings instead of silently dropping source text.
    page_ranges = [span for page in pages for span in page["spans"]]
    ordered = sorted(page_ranges, key=lambda item: item["offset"])
    if any(a["offset"] + a["length"] > b["offset"] for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Source spans from different pages must not overlap")
    outside_pages = _without_tables([{"offset": 0, "length": len(boundaries) - 1}],
                                    _merge_spans(page_ranges))
    if any(content[slice(*_span_range(span, boundaries))].strip() for span in outside_pages):
        raise ValueError("Some extracted text has no page span; cannot preserve page attribution safely")
    for index, table in enumerate(tables):
        spans = table_ranges[index]
        region_pages = {region["pageNumber"] for element in [table] + table["cells"]
                        for region in element.get("boundingRegions", [])}
        page_numbers = sorted(region_pages | {page["pageNumber"] for page in pages
                                              if _related_spans({"spans": spans}, page["spans"])})
        if not page_numbers or not set(page_numbers) <= seen_pages:
            raise ValueError(f"Table {index} has missing or unknown page references")
        text = table_to_text(table["cells"])
        if not text.strip():
            raise ValueError(f"Table {index} has no searchable cell content")
        first_page = page_numbers[0]
        regions = deepcopy(table.get("boundingRegions", []))
        if not regions:
            regions = [deepcopy(region) for cell in table["cells"]
                       for region in cell.get("boundingRegions", [])]
        table_chunks.append({
            **common, "chunk_id": f"table_{uuid4()}", "chunk_type": "table",
            "table_index": index, "page_number": first_page, "page_numbers": page_numbers,
            "section_heading": _heading(paragraphs, first_page, spans[0]["offset"]),
            "content": text, "token_count": count_tokens(text),
            "row_count": table.get("rowCount"), "column_count": table.get("columnCount"),
            "cells": deepcopy(table["cells"]), "spans": deepcopy(spans),
            "bounding_regions": regions, "related_text_chunk_ids": [],
        })
    _link_tables(text_chunks, table_chunks, paragraphs, excluded, pages)
    return {
        "document_id": common["document_id"], "file_name": common["file_name"],
        "chunking": {"text_chunk_size": 400, "text_overlap": 80},
        "text_chunks": text_chunks, "table_chunks": table_chunks,
    }


def save_chunked_json(chunked_document: dict) -> str:
    """Explicitly save chunk_document's output to chunked-documents; return its URL.

    Uses the same connection string and container-creation approach as extraction.
    chunk_document stays pure; the upload handler explicitly calls this function.
    """
    file_name = chunked_document.get("file_name")
    if not isinstance(file_name, str) or not file_name.strip():
        raise ValueError("Chunked output must include the original file_name")
    json_name = str(PurePosixPath(file_name).with_suffix(".json"))
    payload = json.dumps(chunked_document, ensure_ascii=False, indent=2).encode("utf-8")

    load_dotenv("backend/.env")
    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not connection_string:
        raise ValueError("AZURE_STORAGE_CONNECTION_STRING is not configured")

    blob_service = BlobServiceClient.from_connection_string(connection_string)
    container = blob_service.get_container_client("chunked-documents")
    try:
        container.create_container()
    except ResourceExistsError:
        pass
    blob = container.get_blob_client(json_name)
    blob.upload_blob(
        payload,
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )
    return blob.url
