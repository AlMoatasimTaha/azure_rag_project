"""Azure OpenAI embeddings for chunk_document's in-memory output.

AZURE_OPENAI_EMBEDDING_DEPLOYMENT must name the existing deployment of
text-embedding-3-small. This service never persists chunks or vectors.
"""

from copy import deepcopy
import logging
import math
import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
import requests


load_dotenv(Path(__file__).with_name(".env"))
logger = logging.getLogger(__name__)
API_VERSION = "2024-10-21"


def generate_embedding(text: str) -> list[float]:
    """Embed one nonempty string using the configured Azure deployment.

    Raises TypeError/ValueError for invalid input or missing configuration,
    and RuntimeError for API failures or invalid embedding responses. Raw
    API errors are suppressed because they may contain sensitive details.
    """
    if not isinstance(text, str):
        raise TypeError("Embedding input must be a string")
    if not text.strip():
        raise ValueError("Embedding input must not be empty or whitespace")

    names = (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    )
    config = {name: os.getenv(name, "").strip() for name in names}
    missing = [name for name in names if not config[name]]
    if missing:
        raise ValueError("Missing embedding configuration: " + ", ".join(missing))

    endpoint = config["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    deployment = quote(config["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"], safe="")
    url = f"{endpoint}/openai/deployments/{deployment}/embeddings"
    try:
        response = requests.post(
            url,
            params={"api-version": API_VERSION},
            headers={"api-key": config["AZURE_OPENAI_API_KEY"]},
            json={"input": text, "encoding_format": "float"},
            timeout=(10, 60),
            allow_redirects=False,
        )
    except requests.Timeout:
        raise RuntimeError("Azure OpenAI embedding request timed out") from None
    except requests.RequestException:
        raise RuntimeError(
            "Azure OpenAI embedding request failed; check connectivity and endpoint configuration"
        ) from None

    if response.status_code != 200:
        raise RuntimeError(
            f"Azure OpenAI embedding request failed (HTTP {response.status_code}); "
            "check credentials, deployment, input limits, and service availability"
        )
    try:
        payload = response.json()
        data = payload["data"]
        if not isinstance(data, list) or len(data) != 1:
            raise ValueError
        vector = data[0]["embedding"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("Azure OpenAI returned an invalid embedding response") from None

    if not isinstance(vector, list) or not vector:
        raise RuntimeError("Azure OpenAI returned an empty or invalid embedding vector")
    if any(type(value) not in (int, float) for value in vector):
        raise RuntimeError("Azure OpenAI embedding vector contains nonnumeric values")
    try:
        vector = [float(value) for value in vector]
    except (OverflowError, ValueError):
        raise RuntimeError("Azure OpenAI embedding vector contains invalid numeric values") from None
    if not all(math.isfinite(value) for value in vector):
        raise RuntimeError("Azure OpenAI embedding vector contains nonfinite values")

    logger.info("Embedding generated")
    logger.info("Vector dimension: %d", len(vector))
    return vector


def embed_chunks(chunked_data: dict) -> dict:
    """Return a copy of chunk_document's result with content_vector per chunk.

    Pass the dictionary returned by chunk_document directly to this function.
    Only each chunk's content is sent to Azure OpenAI. All envelope fields,
    chunk metadata, and list ordering are preserved; the input is unchanged.
    The caller can pass the returned dictionary to a future Search service
    directly, without an intermediate JSON file or storage write.
    """
    if not isinstance(chunked_data, dict):
        raise TypeError("chunked_data must be a dictionary returned by chunk_document")
    for group in ("text_chunks", "table_chunks"):
        if not isinstance(chunked_data.get(group), list):
            raise ValueError(f"chunked_data must contain a {group} list")
        for index, chunk in enumerate(chunked_data[group]):
            if (not isinstance(chunk, dict)
                    or not isinstance(chunk.get("content"), str)
                    or not chunk["content"].strip()):
                raise ValueError(f"{group}[{index}] must contain nonempty string content")

    embedded_data = deepcopy(chunked_data)
    for group in ("text_chunks", "table_chunks"):
        for chunk in embedded_data[group]:
            logger.info("Embedding chunk: %s", chunk.get("chunk_id", "<unknown>"))
            chunk["content_vector"] = generate_embedding(chunk["content"])
    return embedded_data
