"""Push embedded chunks directly to Azure AI Search; never persist locally.

Nested source metadata is stored as JSON strings for lossless retrieval.
No clients or network calls are made at import time.
"""

from copy import deepcopy
import json
import logging
import math
import os
from pathlib import Path
import re
import sys

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration, HnswParameters, SearchField, SearchFieldDataType, SearchIndex,
    SearchableField, SimpleField, VectorSearch, VectorSearchProfile,
)
from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))
logger = logging.getLogger(__name__)
EMBEDDING_DIMENSION = 1536  # Confirmed by the deployed embedding connection test.
VECTOR_PROFILE_NAME = "rag-vector-profile"
HNSW_CONFIGURATION_NAME = "rag-hnsw"
STRING_FIELDS = (
    "document_id", "file_name", "chunk_type", "section_heading",
    "source_blob_url", "extracted_json_url", "string_index_type",
)
INTEGER_FIELDS = (
    "chunk_index", "page_number", "token_count", "table_index", "row_count", "column_count",
)
JSON_FIELDS = ("spans", "bounding_regions", "cells")
ID_COLLECTION_FIELDS = ("related_table_ids", "related_text_chunk_ids")
RETRIEVAL_TOP_K = 10
RETRIEVAL_FIELDS = (
    "chunk_id", "content", "document_id", "file_name", "chunk_type",
    "page_numbers", "section_heading", "source_blob_url",
)


def delete_document_chunks(document_id: str) -> int:
    """Delete only keys matching document_id; leave the index/schema intact."""
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be nonempty")
    endpoint, credential, index_name = _configuration()
    escaped_id = document_id.replace("'", "''")
    try:
        with SearchClient(endpoint=endpoint, credential=credential, index_name=index_name) as client:
            # Materialize keys before deleting so paging cannot skip changing results.
            keys = [{"chunk_id": match["chunk_id"]} for match in client.search(
                search_text="*", filter=f"document_id eq '{escaped_id}'", select=["chunk_id"],
            )]
            for offset in range(0, len(keys), 1000):
                batch = keys[offset:offset + 1000]
                results = client.delete_documents(documents=batch)
                if (len(results) != len(batch)
                        or {result.key for result in results} != {item["chunk_id"] for item in batch}
                        or any(not result.succeeded for result in results)):
                    raise RuntimeError("Search did not confirm every chunk deletion; retry cleanup")
            return len(keys)
    except ResourceNotFoundError:
        return 0
    except AzureError:
        raise RuntimeError("Search cleanup failed; retry document deletion") from None


def search_chunks(question_vector, document_id: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Return vector-ranked chunks from only the supplied document, without vectors.

    This read-only path never creates or changes an index or stored documents.
    """
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a nonempty string")
    if type(top_k) is not int or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    if not isinstance(question_vector, list) or len(question_vector) != EMBEDDING_DIMENSION:
        raise ValueError("Question embedding must have 1536 dimensions")
    try:
        valid = all(type(value) in (int, float) and math.isfinite(value) for value in question_vector)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Question embedding must contain finite numbers")
    endpoint, credential, index_name = _configuration()
    # Escape OData string literals so IDs cannot change the filter expression.
    escaped_id = document_id.strip().replace("'", "''")
    query = VectorizedQuery(
        vector=question_vector, k_nearest_neighbors=top_k, fields="content_vector",
    )
    try:
        with SearchClient(endpoint=endpoint, credential=credential, index_name=index_name) as client:
            matches = client.search(
                search_text=None, vector_queries=[query],
                filter=f"document_id eq '{escaped_id}'", vector_filter_mode="preFilter",
                select=list(RETRIEVAL_FIELDS), top=top_k,
            )
            return [
                {**{field: match.get(field) for field in RETRIEVAL_FIELDS},
                 "score": match.get("@search.score")}
                for match in matches
            ]
    except AzureError:
        raise RuntimeError(
            "Azure AI Search retrieval failed; check configuration, permissions, and service availability"
        ) from None


def _configuration():
    names = ("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY", "AZURE_SEARCH_INDEX_NAME")
    values = [os.getenv(name, "").strip() for name in names]
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise ValueError("Missing Search configuration: " + ", ".join(missing))
    return values[0], AzureKeyCredential(values[1]), values[2]


def _index_schema(index_name, dimension):
    fields = [
        SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True, hidden=False),
        SearchableField(name="content", type=SearchFieldDataType.String, hidden=False),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True, hidden=False, vector_search_dimensions=dimension,
            vector_search_profile_name=VECTOR_PROFILE_NAME,
        ),
    ]
    fields.extend(SimpleField(name=name, type=SearchFieldDataType.String, hidden=False,
                              filterable=name in ("document_id", "file_name", "chunk_type"))
                  for name in STRING_FIELDS if name != "section_heading")
    fields.append(SearchableField(name="section_heading", type=SearchFieldDataType.String, hidden=False))
    fields.extend(SimpleField(name=name, type=SearchFieldDataType.Int32, hidden=False)
                  for name in INTEGER_FIELDS)
    fields.extend(SimpleField(name=name, type=SearchFieldDataType.String, hidden=False) for name in JSON_FIELDS)
    fields.append(SimpleField(name="page_numbers",
                              type=SearchFieldDataType.Collection(SearchFieldDataType.Int32),
                              filterable=True, hidden=False))
    fields.extend(SimpleField(name=name,
                              type=SearchFieldDataType.Collection(SearchFieldDataType.String), hidden=False)
                  for name in ID_COLLECTION_FIELDS)
    return SearchIndex(
        name=index_name, fields=fields,
        vector_search=VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(
                name=HNSW_CONFIGURATION_NAME,
                parameters=HnswParameters(metric="cosine"),
            )],
            profiles=[VectorSearchProfile(name=VECTOR_PROFILE_NAME,
                                          algorithm_configuration_name=HNSW_CONFIGURATION_NAME)],
        ),
    )


def _validate_index(actual, expected):
    fields = {field.name: field for field in actual.fields}
    if [field.name for field in actual.fields if field.key] != ["chunk_id"]:
        raise ValueError("Existing Search index must use chunk_id as its unique key")
    for required in expected.fields:
        field = fields.get(required.name)
        if field is None or field.type != required.type or field.hidden:
            raise ValueError(f"Existing Search index has an incompatible field: {required.name}")
    for name in ("content", "section_heading"):
        if not fields[name].searchable:
            raise ValueError(f"Existing Search index {name} must be searchable")
    for name in ("document_id", "file_name", "chunk_type", "page_numbers"):
        if not fields[name].filterable:
            raise ValueError(f"Existing Search index {name} must be filterable")
    vector = fields["content_vector"]
    if (not vector.searchable
            or vector.vector_search_dimensions != expected.fields[2].vector_search_dimensions):
        raise ValueError("Existing Search index vector dimension/configuration does not match embeddings")
    search = actual.vector_search
    profiles = {profile.name: profile for profile in (search.profiles or [])} if search else {}
    profile = profiles.get(vector.vector_search_profile_name)
    algorithms = {algorithm.name: algorithm for algorithm in (search.algorithms or [])} if search else {}
    algorithm = algorithms.get(profile.algorithm_configuration_name) if profile else None
    if algorithm is None or algorithm.kind != "hnsw":
        raise ValueError("Existing Search index content_vector must use an HNSW profile")


def ensure_search_index(vector_dimension: int):
    """Create a missing index or validate an existing one without changing it.

    Supply the dimension measured from actual embeddings; there is no default.
    An incompatible existing index requires an explicit migration by the caller.
    """
    if type(vector_dimension) is not int or vector_dimension <= 0:
        raise ValueError("vector_dimension must be a positive integer from an actual embedding")
    endpoint, credential, index_name = _configuration()
    expected = _index_schema(index_name, vector_dimension)
    try:
        with SearchIndexClient(endpoint=endpoint, credential=credential) as client:
            try:
                actual = client.get_index(index_name)
            except ResourceNotFoundError:
                actual = None
            logger.info("Azure AI Search connection: OK")
            logger.info("Index name: %s", index_name)
            logger.info("Vector dimension: %d", vector_dimension)
            if actual is None:
                logger.info("Creating index...")
                try:
                    actual = client.create_index(expected)
                except ResourceExistsError:
                    actual = client.get_index(index_name)
                    print(f"Index {index_name} already exists")
                else:
                    logger.info("Index created successfully")
            else:
                print(f"Index {index_name} already exists")
            _validate_index(actual, expected)
    except AzureError:
        raise RuntimeError(
            "Azure AI Search index setup failed; check endpoint, admin-key permissions, and service availability"
        ) from None
    return actual


def ensure_index_exists():
    """Initialize/check only the index using the confirmed 1536 dimensions.

    Uses the existing SDK/schema logic. Does not embed or upload any documents,
    recreate an existing index, or create any other Search resources.
    """
    return ensure_search_index(EMBEDDING_DIMENSION)


def _prepare_documents(embedded_data):
    if not isinstance(embedded_data, dict):
        raise TypeError("embedded_data must be the dictionary returned by embed_chunks")
    documents, seen, dimension = [], set(), None
    for group in ("text_chunks", "table_chunks"):
        if not isinstance(embedded_data.get(group), list):
            raise ValueError(f"embedded_data must contain a {group} list")
        for index, chunk in enumerate(embedded_data[group]):
            location = f"{group}[{index}]"
            if not isinstance(chunk, dict):
                raise ValueError(f"{location} must be a chunk dictionary")
            key = chunk.get("chunk_id")
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_=-]{1,1024}", key):
                raise ValueError(f"{location} requires a valid Azure Search chunk_id")
            if key in seen:
                raise ValueError("Duplicate chunk_id in embedded_data")
            seen.add(key)
            content = chunk.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"{location} requires nonempty string content")
            vector = chunk.get("content_vector")
            if not isinstance(vector, list) or not vector:
                raise ValueError(f"{location} requires a nonempty content_vector")
            try:
                valid = all(type(value) in (int, float) and math.isfinite(value) for value in vector)
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError(f"{location} vector values must be finite numbers")
            if dimension is None:
                dimension = len(vector)
            if len(vector) != dimension:
                raise ValueError("All content_vector dimensions must match")
            document = {"chunk_id": key, "content": content, "content_vector": deepcopy(vector)}
            for name in (*STRING_FIELDS, *INTEGER_FIELDS, "page_numbers", *ID_COLLECTION_FIELDS):
                # Keep the legacy field in the schema, but never populate checkpoint URLs.
                if name in chunk and name != "extracted_json_url":
                    document[name] = deepcopy(chunk[name])
            for name in JSON_FIELDS:
                if name in chunk:
                    document[name] = json.dumps(chunk[name], ensure_ascii=False, allow_nan=False)
            documents.append(document)
    return documents, dimension


def _batches(documents):
    # Stay below Search's 1,000-action / 16 MiB limits, including overhead.
    batch, size = [], 0
    for document in documents:
        document_size = len(json.dumps(document, ensure_ascii=True, allow_nan=False).encode("utf-8")) + 128
        if document_size > 15 * 1024 * 1024:
            raise ValueError("A Search document exceeds the upload size limit")
        if batch and (len(batch) >= 1000 or size + document_size > 15 * 1024 * 1024):
            yield batch
            batch, size = [], 0
        batch.append(document)
        size += document_size
    if batch:
        yield batch


def index_chunks(embedded_data: dict) -> list:
    """Upload both chunk lists without mutating input; return SDK IndexingResults.

    Raises on API or per-document failures; earlier batches may be indexed.
    Repeating the same chunk IDs replaces those documents (upload semantics).
    Empty chunk lists return [] without creating an index or calling Azure.
    """
    documents, dimension = _prepare_documents(embedded_data)
    if not documents:
        return []
    batches = list(_batches(documents))  # Validate sizes before any network write.
    ensure_search_index(dimension)
    endpoint, credential, index_name = _configuration()
    results = []
    try:
        with SearchClient(endpoint=endpoint, index_name=index_name, credential=credential) as client:
            for batch in batches:
                for document in batch:
                    logger.info("Indexing chunk: %s", document["chunk_id"])
                uploaded = client.upload_documents(documents=batch)
                if (len(uploaded) != len(batch)
                        or {result.key for result in uploaded} != {doc["chunk_id"] for doc in batch}
                        or any(not result.succeeded for result in uploaded)):
                    raise RuntimeError(
                        "Azure AI Search did not index every chunk successfully; "
                        "some chunks may already be indexed. Check schema, quotas, and service availability"
                    )
                results.extend(uploaded)
    except AzureError:
        raise RuntimeError(
            "Azure AI Search upload failed; some chunks may already be indexed. "
            "Check permissions, schema, quotas, and service availability"
        ) from None
    logger.info("Indexed successfully (%d chunks)", len(results))
    return results


if __name__ == "__main__":
    # Show only this service's messages, avoiding SDK HTTP logs and secrets.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        ensure_index_exists()
    except (ValueError, RuntimeError) as error:
        logger.error("Index initialization failed: %s", error)
        raise SystemExit(1) from None
    except Exception:
        logger.error("Index initialization failed unexpectedly; check backend configuration and connectivity")
        raise SystemExit(1) from None
