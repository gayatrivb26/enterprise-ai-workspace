"""
app/storage.py — durable storage for uploaded source files.

Ingestion is not guaranteed to succeed on the first attempt (a provider blip,
a Chroma restart, an out-of-memory PDF), and a "Retry" button that cannot
actually retry is worse than no button at all. Keeping the original bytes
makes retry real, and also makes re-chunking an existing corpus possible when
the chunking strategy changes — without asking users to re-upload anything.

Files are content-addressed by document id under UPLOAD_DIR. In compose the
ai-service directory is bind-mounted, so they survive container restarts;
point UPLOAD_DIR at a volume or object store for a real deployment.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads"))


def _dir_for(document_id: str) -> Path:
    return UPLOAD_DIR / document_id


def save(document_id: str, filename: str, data: bytes) -> Path:
    target_dir = _dir_for(document_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    # Store under a fixed name so retrieval never has to guess; the real
    # filename already lives in the documents row.
    path = target_dir / "source.bin"
    path.write_bytes(data)
    (target_dir / "name.txt").write_text(filename, encoding="utf-8")
    return path


def load(document_id: str) -> bytes | None:
    path = _dir_for(document_id) / "source.bin"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("Could not read stored file for %s: %s", document_id, e)
        return None


def exists(document_id: str) -> bool:
    return (_dir_for(document_id) / "source.bin").is_file()


def delete(document_id: str) -> None:
    try:
        shutil.rmtree(_dir_for(document_id), ignore_errors=True)
    except OSError as e:
        log.warning("Could not delete stored file for %s: %s", document_id, e)
