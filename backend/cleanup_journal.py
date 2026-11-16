"""Local compensation outbox, independent of Azure availability.

Only resource coordinates and safe failure details are stored. Atomic replaces
keep pending work recoverable after a backend restart; nothing is uploaded here.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from urllib.parse import urlsplit, urlunsplit

CLEANUP_DIR = Path(__file__).with_name(".cleanup-pending")
_lock = RLock()


def _path(document_id):
    return CLEANUP_DIR / (sha256(document_id.encode("utf-8")).hexdigest() + ".json")


def save_pending_cleanup(record):
    payload = dict(record)
    for field in ("blob_url", "extracted_json_url"):
        if payload.get(field):
            parsed = urlsplit(payload[field])
            payload[field] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if payload.get("artifact_urls"):
        payload["artifact_urls"] = [urlunsplit((*urlsplit(url)[:3], "", "")) for url in payload["artifact_urls"]]
    with _lock:
        CLEANUP_DIR.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=CLEANUP_DIR, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _path(record["document_id"]))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def load_pending_cleanups():
    with _lock:
        if not CLEANUP_DIR.exists():
            return []
        records = []
        for path in sorted(CLEANUP_DIR.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record.get("document_id"), str) or not record["document_id"]:
                raise ValueError("Invalid cleanup journal entry")
            records.append(record)
        return records


def pending_cleanup_ids():
    return {record["document_id"] for record in load_pending_cleanups()}


def remove_pending_cleanup(document_id):
    with _lock:
        _path(document_id).unlink(missing_ok=True)
