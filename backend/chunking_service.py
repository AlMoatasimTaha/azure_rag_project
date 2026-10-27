"""In-memory chunking for the extraction_service result envelope.

Offsets in output spans use Azure's original stringIndexType. Bounding polygons
describe source elements, not an estimated crop around individual tokens.
"""

from bisect import bisect_left
from copy import deepcopy
from functools import lru_cache
from statistics import median
from uuid import uuid4

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


FURNITURE = {"pageHeader", "pageFooter", "pageNumber"}


def _cell_fragment(cell, start, end, result):
    """Map a long cell's fragment back to its original Azure span units."""
    fragment = deepcopy(cell)
    fragment["content"] = cell["content"][start:end]
    boundaries = _index_boundaries(result["content"], result["stringIndexType"])
    reverse = {char: i for i, char in enumerate(boundaries) if char is not None}
    source = [(i, result["content"][i]) for span in cell.get("spans", [])
              for i in range(*_span_range(span, boundaries)) if not result["content"][i].isspace()]
    target = [(i, char) for i, char in enumerate(cell["content"]) if not char.isspace()]
    if "".join(char for _, char in source) != "".join(char for _, char in target):
        raise ValueError("Cannot safely map an oversized cell to its source spans")
    selected = {source[pos][0] for pos, (i, _) in enumerate(target) if start <= i < end}
    spans = []
    for span in cell.get("spans", []):
        left, right = _span_range(span, boundaries)
        positions = selected.intersection(range(left, right))
        if positions:
            a, b = reverse[min(positions)], reverse[max(positions) + 1]
            spans.append({"offset": a, "length": b - a})
    fragment["spans"] = spans
    return fragment


def _split_table(table, result, limit=400):
    """Pack complete rows, repeating column headers and merged-cell context.

    Preserve original row/column indices. A single oversized row splits by cells,
    then by Unicode-safe cell fragments if needed. Small row labels repeat.
    """
    cells = table["cells"]
    headers = [c for c in cells if c.get("kind") == "columnHeader"]
    header_ids = {id(c) for c in headers}

    def render(selected):
        return table_to_text(selected, header_cells=headers)

    def part(selected, value=None):
        return {"cells": deepcopy(selected), "content": value if value is not None else render(selected),
                "spans": _merge_spans([s for c in selected for s in c.get("spans", [])])}

    full_text = render(cells)
    if count_tokens(full_text) <= limit:
        return [part(cells, full_text)]
    if headers and count_tokens(render(headers)) >= limit:
        raise ValueError("Table headers alone consume the token budget; cannot repeat them safely")
    body = [c for c in cells if id(c) not in header_ids]
    rows = sorted({c["rowIndex"] for c in body})

    def select_rows(row_numbers):
        # A merged cell continues to describe the rows it spans.
        return headers + [c for c in body if any(c["rowIndex"] <= row < c["rowIndex"] + c.get("rowSpan", 1)
                                                for row in row_numbers)]

    def split_row(row):
        selected = select_rows([row])
        values = [c for c in selected if id(c) not in header_ids]
        values.sort(key=lambda c: c["columnIndex"])
        # Short leftmost cells usually identify the row (week, assessment, ID...).
        identifiers = values[:1] if len(values) > 1 and values[0].get("content", "").strip() and count_tokens(values[0]["content"]) <= 40 else []
        base = headers + identifiers
        remaining = values[1:] if identifiers else values
        prefix = f"Row {row + 1}\n"

        def row_text(selected):
            return prefix + render(selected)

        if count_tokens(row_text(base)) >= limit:
            raise ValueError("Table row context consumes the token budget")
        fragments, pending = [], []
        for cell in remaining:
            if count_tokens(row_text(base + pending + [cell])) <= limit:
                pending.append(cell)
                continue
            if pending:
                fragments.append(part(base + pending, row_text(base + pending)))
                pending = []
            if count_tokens(row_text(base + [cell])) <= limit:
                pending = [cell]
                continue
            budget = limit
            while budget > 0:
                windows = list(_text_windows(cell.get("content", ""), budget, 0))
                pieces = [_cell_fragment(cell, start, end, result) for start, end, _ in windows]
                overflow = max((count_tokens(row_text(base + [p])) - limit for p in pieces), default=0)
                if overflow <= 0:
                    break
                budget -= max(1, overflow)
            if budget <= 0:
                raise ValueError("A table cell cannot fit with its required column and row labels")
            fragments.extend(part(base + [p], row_text(base + [p])) for p in pieces)
        if pending:
            fragments.append(part(base + pending, row_text(base + pending)))
        return fragments

    parts, pending_rows = [], []
    for row in rows:
        candidate = select_rows(pending_rows + [row])
        if count_tokens(render(candidate)) <= limit:
            pending_rows.append(row)
            continue
        if pending_rows:
            parts.append(part(select_rows(pending_rows)))
            pending_rows = []
        if count_tokens(render(select_rows([row]))) <= limit:
            pending_rows = [row]
        else:
            parts.extend(split_row(row))
    if pending_rows:
        parts.append(part(select_rows(pending_rows)))
    if not rows:
        raise ValueError("Oversized header-only table cannot be split between data rows")
    if not parts or any(count_tokens(p["content"]) > limit for p in parts):
        raise ValueError("Table splitting did not satisfy the token limit")
    return parts


def _box(element):
    regions = element.get("boundingRegions", [])
    if not regions or not regions[0].get("polygon"):
        raise ValueError("This layout experiment requires source bounding polygons")
    polygon = regions[0]["polygon"]
    return (regions[0]["pageNumber"], min(polygon[::2]), min(polygon[1::2]),
            max(polygon[::2]), max(polygon[1::2]))


def _font_height(paragraph, result):
    page_number, _, top, _, bottom = _box(paragraph)
    lines = [line for page in result["pages"] if page["pageNumber"] == page_number
             for line in page.get("lines", []) if _related_spans(line, paragraph["spans"])]
    heights = [max(line["polygon"][1::2]) - min(line["polygon"][1::2]) for line in lines]
    return median(heights) if heights else bottom - top


def _heading_groups(result, excluded, boundaries):
    """Join stacked heading fragments using source adjacency and layout."""
    groups = []
    for paragraph in sorted(result["paragraphs"], key=lambda p: p["spans"][0]["offset"]):
        if paragraph.get("role") not in {"title", "sectionHeading"} or _related_spans(paragraph, excluded):
            continue
        if paragraph["content"].strip().isdecimal():
            continue  # A numbered badge is list content, not a section boundary.
        b, size = _box(paragraph), _font_height(paragraph, result)
        join = False
        if groups:
            previous = groups[-1]
            p = previous["headings"][-1]
            pb = _box(p)
            end = max(s["offset"] + s["length"] for s in p["spans"])
            start = paragraph["spans"][0]["offset"]
            gap = result["content"][boundaries[end]:boundaries[start]]
            join = (paragraph.get("role") == p.get("role") and b[0] == pb[0]
                    and not gap.strip() and abs(b[1] - pb[1]) < size * 1.5
                    and 0 <= b[2] - pb[4] <= max(size, previous["size"]) * 1.1)
        if join:
            previous["headings"].append(paragraph)
            if size >= previous["size"] * .9:
                previous["label"] += " " + paragraph["content"]
            previous["size"] = max(previous["size"], size)
            previous["_box"] = (b[0], min(previous["_box"][1], b[1]), previous["_box"][2],
                               max(previous["_box"][3], b[3]), b[4])
        else:
            groups.append({"label": paragraph["content"], "headings": [paragraph],
                           "_box": b, "size": size, "role": paragraph.get("role")})
    return groups


def _parent_for(element, groups, widths):
    """Choose the latest heading in the nearest column to the left."""
    page, left, top, _, _ = _box(element)
    tolerance = widths[page] * .05
    candidates = [g for g in groups if g["_box"][0] == page and g["_box"][2] <= top + .02
                  and (g["_box"][1] <= left + tolerance or g["role"] == "title")]
    if not candidates:
        return None
    sections = [g for g in candidates if g["role"] != "title"]
    if sections:
        closest = min(abs(g["_box"][1] - left) for g in sections)
        candidates = [g for g in sections if abs(g["_box"][1] - left) <= closest + tolerance]
    return max(candidates, key=lambda g: g["_box"][2])


def _ordered_blocks(paragraphs, result, widths):
    """Keep card contents together, then read cards by row and column."""
    columns = []
    for paragraph in sorted(paragraphs, key=lambda p: (_box(p)[0], _box(p)[1], _box(p)[2])):
        b = _box(paragraph)
        column = next((c for c in columns if c[0][0] == b[0]
                       and abs(c[0][1] - b[1]) <= widths[b[0]] * .045), None)
        if column is None:
            column = [b, []]
            columns.append(column)
        column[1].append(paragraph)
    blocks = []
    for _, items in columns:
        items.sort(key=lambda p: _box(p)[2])
        unit, bottom = [], None
        for paragraph in items:
            b = _box(paragraph)
            if unit and b[2] - bottom > _font_height(paragraph, result) * 2:
                blocks.append(unit)
                unit = []
            unit.append(paragraph)
            bottom = max(bottom or b[4], b[4]) if unit else b[4]
        if unit:
            blocks.append(unit)
    blocks.sort(key=lambda unit: (_box(unit[0])[0], round(_box(unit[0])[2] / .12), _box(unit[0])[1]))
    return [sorted(unit, key=lambda p: p["spans"][0]["offset"]) for unit in blocks]


def chunk_document(extracted_json: dict) -> dict:
    """Group extracted content by headings/layout; cap text and tables at 400 tokens.

    Keep the existing output schema and source URL. Large tables split by rows,
    repeating column labels; oversized rows split by cells or cell fragments.
    The input envelope is never modified. No storage or extraction calls occur.
    """
    _validate_extraction(extracted_json)
    return _build_chunks(extracted_json)


def _validate_extraction(document):
    result = document.get("extraction_result") if isinstance(document, dict) else None
    if not isinstance(result, dict):
        raise ValueError("Expected the saved JSON envelope with extraction_result")
    content, pages = result.get("content"), result.get("pages")
    if not isinstance(content, str) or not isinstance(pages, list):
        raise ValueError("extraction_result must contain content and pages")
    if not isinstance(result.get("paragraphs"), list):
        raise ValueError("Layout chunking requires extracted paragraphs")
    boundaries = _index_boundaries(content, result.get("stringIndexType"))
    seen, spans = set(), []
    for page in pages:
        number = page.get("pageNumber")
        if type(number) is not int or number < 1 or number in seen:
            raise ValueError("Each page must have a unique positive pageNumber")
        if not isinstance(page.get("spans"), list):
            raise ValueError(f"Page {number} is missing spans")
        if not isinstance(page.get("width"), (int, float)) or page["width"] <= 0:
            raise ValueError(f"Page {number} needs a positive layout width")
        seen.add(number)
        spans.extend(page["spans"])
    for span in spans:
        _span_range(span, boundaries)
    ordered = sorted(spans, key=lambda s: s["offset"])
    if any(a["offset"] + a["length"] > b["offset"] for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Source page spans must not overlap")
    outside = _without_tables([{"offset": 0, "length": len(boundaries) - 1}], _merge_spans(spans))
    if any(content[slice(*_span_range(s, boundaries))].strip() for s in outside):
        raise ValueError("Some extracted text has no page span")
    elements = list(result["paragraphs"])
    for table in result.get("tables", []):
        if not table.get("cells") or not (table.get("spans") or any(c.get("spans") for c in table["cells"])):
            raise ValueError("Each table needs cells and nonempty source spans")
        elements.extend([table] + table["cells"])
    for element in elements:
        for span in element.get("spans", []):
            _span_range(span, boundaries)
        if any(region.get("pageNumber") not in seen for region in element.get("boundingRegions", [])):
            raise ValueError("An extracted element references an unknown page")


def _build_chunks(document, limit=400):
    result = document["extraction_result"]
    content = result["content"]
    boundaries = _index_boundaries(content, result["stringIndexType"])
    reverse = {value: i for i, value in enumerate(boundaries) if value is not None}
    widths = {page["pageNumber"]: page["width"] for page in result["pages"]}
    tables = result.get("tables", [])
    table_spans = [_merge_spans(table.get("spans", []) + [s for c in table["cells"] for s in c.get("spans", [])])
                   for table in tables]
    excluded = _merge_spans([s for spans in table_spans for s in spans] +
                           [s for p in result["paragraphs"] if p.get("role") in FURNITURE for s in p["spans"]])
    candidates = _heading_groups(result, excluded, boundaries)
    groups = []
    # Layout order prevents extraction's interleaved columns from defining sections.
    for candidate in sorted(candidates, key=lambda g: (g["_box"][0], g["_box"][2], g["_box"][1])):
        ancestor = _parent_for(candidate["headings"][0], groups, widths)
        nested = (ancestor and ancestor["role"] != "title" and candidate["role"] != "title"
                  and candidate["size"] < ancestor["size"] * .9
                  and (candidate["size"] < ancestor["size"] * .75
                       or candidate["_box"][1] - ancestor["_box"][1] > widths[candidate["_box"][0]] * .035))
        if not nested:
            candidate.update(items=[], tables=[], chunks=[])
            groups.append(candidate)
    groups.sort(key=lambda g: min(p["spans"][0]["offset"] for p in g["headings"]))
    owned_headings = {id(p): g for g in groups for p in g["headings"]}
    for paragraph in result["paragraphs"]:
        if paragraph.get("role") in FURNITURE or _related_spans(paragraph, [s for spans in table_spans for s in spans]):
            continue
        group = owned_headings.get(id(paragraph)) or _parent_for(paragraph, groups, widths)
        if group is None:
            group = {"label": None, "headings": [], "_box": _box(paragraph), "size": 0,
                     "role": None, "items": [], "tables": [], "chunks": []}
            groups.append(group)
        group["items"].append(paragraph)
    for index, table in enumerate(tables):
        group = _parent_for(table, groups, widths)
        if group is not None:
            group["tables"].append(index)

    common = {"document_id": document["document_id"], "file_name": document["file_name"],
              "source_blob_url": document["blob_url"], "string_index_type": result["stringIndexType"]}
    text_chunks, table_chunks = [], []
    for group in groups:
        heading_ids = {id(p) for p in group["headings"]}
        body = [p for p in group["items"] if id(p) not in heading_ids]
        if not body and group["tables"]:
            continue  # The table carries this heading; avoid a heading-only text chunk.
        units = ([group["headings"]] if group["headings"] else []) + _ordered_blocks(body, result, widths)
        parts, segments, position = [], [], 0
        for unit in units:
            for paragraph in unit:
                for span in paragraph["spans"]:
                    a, b = _span_range(span, boundaries)
                    if parts:
                        parts.append("\n\n")
                        position += 2
                    value = content[a:b]
                    segments.append((position, position + len(value), a))
                    parts.append(value)
                    position += len(value)
        text = "".join(parts)
        # Each source paragraph is a preferred split boundary.
        cuts = [0] + [end for _, end, _ in segments]
        blocks = []
        for a, b in zip(cuts, cuts[1:]):
            blocks.extend((a + left, a + right) for left, right, _ in _text_windows(text[a:b], limit, 0))
        windows, start, end = [], None, None
        for left, right in blocks:
            if start is not None and count_tokens(text[start:right]) <= limit:
                end = right
            else:
                if start is not None:
                    windows.append((start, end))
                start, end = left, right
        if start is not None:
            windows.append((start, end))
        for start, end in windows:
            value = text[start:end].strip()
            if not value:
                continue
            spans = []
            for left, right, source in segments:
                a, b = max(start, left), min(end, right)
                if a < b:
                    offset, stop = reverse[source + a - left], reverse[source + b - left]
                    spans.append({"offset": offset, "length": stop - offset})
            selected = [p for p in group["items"] if _related_spans(p, spans)]
            regions = [r for p in selected for r in p.get("boundingRegions", [])]
            pages = sorted({r["pageNumber"] for r in regions})
            text_chunks.append({**common, "chunk_id": f"text_{uuid4()}", "chunk_type": "text",
                "chunk_index": len(text_chunks), "page_number": pages[0], "page_numbers": pages,
                "content": value, "section_heading": group["label"], "token_count": count_tokens(value),
                "spans": spans, "bounding_regions": deepcopy(regions), "related_table_ids": []})
            group["chunks"].append(text_chunks[-1])
    for index, table in enumerate(tables):
        group = next((g for g in groups if index in g["tables"]), None)
        related = group["chunks"] if group else []
        parts = _split_table(table, result, limit)
        for part_index, part in enumerate(parts):
            spans = part["spans"] if len(parts) > 1 else table_spans[index]
            if part_index == 0 and group and not any(id(p) not in {id(h) for h in group["headings"]} for p in group["items"]):
                spans = _merge_spans(spans + [s for p in group["headings"] for s in p["spans"]])
            regions = [r for cell in part["cells"] for r in cell.get("boundingRegions", [])]
            if not regions:
                regions = table.get("boundingRegions", [])
            pages = sorted({r["pageNumber"] for r in regions} | {p["pageNumber"] for p in result["pages"]
                           if _related_spans({"spans": spans}, p["spans"])})
            value = part["content"]
            chunk_id = f"table_{uuid4()}"
            table_chunks.append({**common, "chunk_id": chunk_id, "chunk_type": "table", "table_index": index,
                "page_number": pages[0], "page_numbers": pages, "section_heading": group["label"] if group else None,
                "content": value, "token_count": count_tokens(value), "row_count": table.get("rowCount"),
                "column_count": table.get("columnCount"), "cells": part["cells"], "spans": deepcopy(spans),
                "bounding_regions": deepcopy(regions), "related_text_chunk_ids": [c["chunk_id"] for c in related]})
            for chunk in related:
                chunk["related_table_ids"].append(chunk_id)
    output = {"document_id": common["document_id"], "file_name": common["file_name"],
              "chunking": {"text_chunk_size": limit, "text_overlap": 0},
              "text_chunks": text_chunks, "table_chunks": table_chunks}
    _verify(document, output)
    return output


def _verify(document, output):
    result = document["extraction_result"]
    boundaries = _index_boundaries(result["content"], result["stringIndexType"])
    covered = set()
    for chunk in output["text_chunks"] + output["table_chunks"]:
        if not chunk["content"].strip() or chunk["token_count"] != count_tokens(chunk["content"]):
            raise ValueError("A chunk has missing content or an incorrect token count")
        if chunk["token_count"] > output["chunking"]["text_chunk_size"]:
            raise ValueError("A chunk exceeds the token limit")
        if chunk["chunk_type"] == "text":
            restored = "".join(result["content"][slice(*_span_range(s, boundaries))] for s in chunk["spans"])
            if "".join(restored.split()) != "".join(chunk["content"].split()):
                raise ValueError("Chunk text does not match its source spans")
        for span in chunk["spans"]:
            covered.update(range(*_span_range(span, boundaries)))
    furniture = [s for p in result["paragraphs"] if p.get("role") in FURNITURE for s in p["spans"]]
    removed = {i for s in furniture for i in range(*_span_range(s, boundaries))}
    missing = {i for i, c in enumerate(result["content"]) if not c.isspace()} - covered - removed
    # Selection marks are extraction artifacts outside paragraph/table spans.
    for marker in (":selected:", ":unselected:"):
        pos = result["content"].find(marker)
        while pos >= 0:
            missing.difference_update(range(pos, pos + len(marker)))
            pos = result["content"].find(marker, pos + 1)
    if missing:
        raise ValueError(f"{document['file_name']}: {len(missing)} meaningful extracted characters uncovered")
    for chunk in output["text_chunks"]:
        if any(_related_spans({"spans": chunk["spans"]}, [s]) for s in furniture):
            raise ValueError("A text chunk includes a marked page header/footer")
