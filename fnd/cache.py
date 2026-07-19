"""Content-addressed PDF structure cache.

Stores serialised ``Chunk`` lists produced by the structuring pipeline
(``pymupdf4llm`` primary, ``docling`` fallback for image-tables) keyed
by ``sha256(file_bytes) || extractor_signature`` under
``$XDG_CACHE_HOME/fnd/pdf-structure/<shard>/<key>.json``. Lookup happens
at the top of an extractor; on hit, extraction is skipped entirely.

Phase 2 of the real-PDF-support workstream. See
``docs/plans/2026-05-20-real-pdf-support-bakeoff.md`` for the design
rationale — content-addressed beats mtime+path because mtime drifts on
rsync / Dropbox / Syncthing, and per-PDF extraction is multi-second so
the one-time sha256 cost (~10ms for a 5MB file on M1 Max) is rounding
error.

History: directory was previously ``$XDG_CACHE_HOME/fnd/extraction/``;
class previously ``ExtractionCache``. Both renamed for clarity — this
is fnd's PDF structure cache, not a generic extractor's. Old directory
is migrated on first launch by :func:`_migrate_legacy_cache_dir` and
``ExtractionCache`` remains as a deprecated alias for one release.
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

# Old directory name (kept for one-time migration on first launch).
_LEGACY_CACHE_DIRNAME = "extraction"
_CACHE_DIRNAME = "pdf-structure"


def default_cache_dir() -> Path:
    """Return fnd's PDF structure cache directory.

    On first launch where the legacy ``extraction/`` directory exists,
    rename it to ``pdf-structure/`` so users don't lose their cache."""
    root = Path(user_cache_dir("fnd"))
    new_dir = root / _CACHE_DIRNAME
    legacy = root / _LEGACY_CACHE_DIRNAME
    if legacy.exists() and not new_dir.exists():
        with contextlib.suppress(OSError):
            legacy.rename(new_dir)
    return new_dir


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Stream a file's bytes through sha256. Single-pass, bounded memory."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _safe_mtime(path: Path) -> float:
    """``path.stat().st_mtime`` or 0.0 if the file vanished underneath us."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class PdfStructureCache:
    """File-backed cache of structured-PDF chunks.

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
        # Per-instance counters for indexer-runner progress reporting.
        # Not thread-safe — caller should snapshot before reading from
        # another task.
        self.hits = 0
        self.misses = 0

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
            self.misses += 1
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None
        if blob.get("schema_version") != CACHE_SCHEMA_VERSION:
            self.misses += 1
            return None
        try:
            chunks = [_chunk_from_dict(d) for d in blob.get("chunks", [])]
        except (KeyError, TypeError, ValueError):
            self.misses += 1
            return None
        self.hits += 1
        return chunks

    def get_any_for_content(self, content_sha256: str) -> list[Chunk] | None:
        """Return cached chunks for this content under ANY signature.

        Durable reuse: when the current-signature key misses (a
        TEXTURE_VERSION bump, or a pre-versioning entry), reuse whatever
        prior texturising exists for the same file bytes rather than
        redoing the multi-second extraction. Newest entry wins. The caller
        does NOT re-key the result, so the file stays "outdated" until an
        explicit Re-texturise pass refreshes it under the current signature.
        """
        shard_dir = self.root / content_sha256[:2]
        candidates = (
            # Guard stat(): an entry can be deleted (cache prune / manual
            # cleanup) between glob and stat — missing → sorts oldest, the
            # subsequent get() then misses cleanly rather than crashing.
            sorted(shard_dir.glob(f"{content_sha256}--*.json"), key=_safe_mtime, reverse=True)
            if shard_dir.exists()
            else []
        )
        for entry in candidates:
            chunks = self.get(entry.stem)  # reuses get()'s decode + hit/miss counters
            if chunks is not None:
                return chunks
        if not candidates:
            self.misses += 1
        return None

    def forget_content(self, content_sha256: str) -> int:
        """Delete every cache entry for this content, under any signature.
        Used by a literal Rebuild so the file's texturing is genuinely
        removed (not just overwritten), leaving no stale entry behind.
        Returns the number of entries removed."""
        shard_dir = self.root / content_sha256[:2]
        if not shard_dir.exists():
            return 0
        removed = 0
        for entry in list(shard_dir.glob(f"{content_sha256}--*.json")):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def count_orphans(self, live_content_shas: set[str]) -> int:
        """How many entries reference content NOT in ``live_content_shas``
        (files no longer on disk). Read-only counterpart of
        :meth:`prune_orphans`."""
        if not self.root.exists():
            return 0
        return sum(
            1
            for _root, _dirs, files in os.walk(self.root)
            for f in files
            if f.endswith(".json") and "--" in f and f.split("--", 1)[0] not in live_content_shas
        )

    def prune_orphans(self, live_content_shas: set[str]) -> int:
        """Delete entries whose content hash is NOT in ``live_content_shas``
        — texturings for files no longer present under any collection's
        sources (removed, renamed to different bytes, or de-configured).
        Content-addressed, so the caller passes the shas of files that
        still exist. Returns the number of entries removed."""
        if not self.root.exists():
            return 0
        removed = 0
        for root_dir, _dirs, files in os.walk(self.root):
            for f in files:
                if not f.endswith(".json") or "--" not in f:
                    continue
                sha = f.split("--", 1)[0]
                if sha not in live_content_shas:
                    with contextlib.suppress(OSError):
                        (Path(root_dir) / f).unlink()
                        removed += 1
        return removed

    def promote_current_engine_entries(
        self, *, current_sig: str, current_cfg_marker: str
    ) -> tuple[int, int]:
        """Re-key entries produced by the CURRENT engine but under the old
        signature FORMAT to ``current_sig``. An entry counts as current-engine
        iff its signature contains ``current_cfg_marker`` (e.g. ``cfg-0cc6be52``);
        only the key format changed, so it is NOT outdated. Entries from older
        configs are left untouched so they correctly read as outdated.

        Idempotent. Returns ``(migrated, failed)``. The caller should only
        mark the migration done (write its sentinel) when ``failed == 0`` —
        otherwise a partial promotion would permanently misclassify the
        un-promoted current-engine entries as outdated. The signature strings
        are passed in so this module stays independent of
        :mod:`fnd.extract.pdf`.
        """
        if not self.root.exists():
            return 0, 0
        migrated = 0
        failed = 0
        # Materialise the listing first: renaming/unlinking entries while
        # iterating the rglob generator can skip or double-visit files.
        for entry in list(self.root.rglob("*.json")):
            stem = entry.stem
            if "--" not in stem:
                continue
            _sha, sig = stem.split("--", 1)
            if sig == current_sig or current_cfg_marker not in sig:
                continue
            target = self.entry_path(f"{_sha}--{current_sig}")
            if target.exists():
                with contextlib.suppress(OSError):
                    entry.unlink()  # dedup: current already present
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(entry, target)
                migrated += 1
            except OSError:
                failed += 1
        return migrated, failed

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
        # .get() not [] — the cache is durable and entries written before
        # these fields existed must still load.
        created=d.get("created", 0),
        inode_changed=d.get("inode_changed", 0),
        page=d.get("page", 0),
        page_label=d.get("page_label", ""),
        slide=d.get("slide", 0),
        line=d.get("line", 0),
        heading_path=d.get("heading_path", ""),
        title=d.get("title", ""),
        author=d.get("author", ""),
        chunk_seq=d.get("chunk_seq", 0),
    )


# Backwards-compatible alias. Use ``PdfStructureCache`` in new code.
ExtractionCache = PdfStructureCache


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "ExtractionCache",
    "PdfStructureCache",
    "default_cache_dir",
    "sha256_file",
]
