"""Content-addressed extraction artifact cache.

Stores serialised ``Chunk`` lists keyed by
``sha256(file_bytes) || extractor_signature`` under
``$XDG_CACHE_HOME/fnd/extraction/<shard>/<key>.json``. Lookup happens
at the top of an extractor; on hit, extraction is skipped entirely.

Phase 2 of the real-PDF-support workstream. See
``docs/plans/2026-05-20-real-pdf-support-bakeoff.md`` for the design
rationale — content-addressed beats mtime+path because mtime drifts on
rsync / Dropbox / Syncthing, and per-PDF extraction is multi-second so
the one-time sha256 cost (~10ms for a 5MB file on M1 Max) is rounding
error.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir

from fnd.extract.base import Block, Chunk

CACHE_SCHEMA_VERSION = 1


def default_cache_dir() -> Path:
    return Path(user_cache_dir("fnd")) / "extraction"


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Stream a file's bytes through sha256. Single-pass, bounded memory."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


class ExtractionCache:
    """File-backed cache of extracted Chunk lists.

    Storage layout: ``<root>/<first-2-of-key>/<key>.json``. The
    shard prefix keeps any one directory below typical filesystem
    inode-list limits (256 shards is plenty for ~10K-file corpora).

    Writes are atomic via ``os.replace`` from a tmpfile in the same
    directory, so a Ctrl+C during the write either leaves the
    previous entry intact (most likely) or no entry at all (worst
    case the next extraction recomputes — still correct).
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_cache_dir()

    @staticmethod
    def build_key(*, content_sha256: str, extractor_signature: str) -> str:
        """Compose a cache key from a content hash and extractor identity.

        ``extractor_signature`` should encode everything that could
        change the extractor's output: package versions + config-shaping
        flags. Different signature → different key → independent
        cache entry; old entries are unreachable but harmless until
        ``fnd cache prune`` runs.
        """
        return f"{content_sha256}--{extractor_signature}"

    def entry_path(self, key: str) -> Path:
        """Where a given key's blob would live on disk."""
        # First 2 hex chars of the sha256 prefix = 256 shards.
        shard = key[:2]
        return self.root / shard / f"{key}.json"

    def get(self, key: str) -> list[Chunk] | None:
        """Return the cached chunks for `key`, or None on miss / corrupt.

        Corrupt entries (truncated JSON, mismatched schema_version,
        deserialisation error) silently miss; the caller re-extracts
        and overwrites on next put().
        """
        path = self.entry_path(key)
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if blob.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        try:
            return [_chunk_from_dict(d) for d in blob.get("chunks", [])]
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, key: str, chunks: list[Chunk]) -> None:
        """Write `chunks` to the cache atomically.

        If the write fails partway through (disk full, interrupt), the
        partial file is cleaned up so the next read either sees the
        previous entry (if there was one) or no entry at all.
        """
        path = self.entry_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "chunks": [_chunk_to_dict(c) for c in chunks],
        }
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def total_size_bytes(self) -> int:
        """Sum on-disk bytes of all entries. Used by `fnd cache status`."""
        if not self.root.exists():
            return 0
        total = 0
        for root, _dirs, files in os.walk(self.root):
            for f in files:
                if f.endswith(".json"):
                    with contextlib.suppress(OSError):
                        total += (Path(root) / f).stat().st_size
        return total

    def entry_count(self) -> int:
        if not self.root.exists():
            return 0
        n = 0
        for _root, _dirs, files in os.walk(self.root):
            n += sum(1 for f in files if f.endswith(".json"))
        return n


def _chunk_to_dict(c: Chunk) -> dict[str, Any]:
    """asdict() handles nested Block dataclasses recursively."""
    return asdict(c)


def _chunk_from_dict(d: dict[str, Any]) -> Chunk:
    """Reconstruct a Chunk from a previously-serialised dict.

    body_struct comes back as a list of dicts and needs explicit
    re-hydration into Block dataclasses (which are frozen, so we use
    the constructor directly).
    """
    blocks = [Block(kind=b["kind"], text=b["text"]) for b in d.get("body_struct", [])]
    return Chunk(
        parent_id=d["parent_id"],
        path=d["path"],
        mtime=d["mtime"],
        kind=d["kind"],
        body=d.get("body", ""),
        body_struct=blocks,
        body_md=d.get("body_md", ""),
        page=d.get("page", 0),
        page_label=d.get("page_label", ""),
        slide=d.get("slide", 0),
        line=d.get("line", 0),
        heading_path=d.get("heading_path", ""),
        title=d.get("title", ""),
        author=d.get("author", ""),
        chunk_seq=d.get("chunk_seq", 0),
    )


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "ExtractionCache",
    "default_cache_dir",
    "sha256_file",
]
