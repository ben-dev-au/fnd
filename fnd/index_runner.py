"""Async indexer runner that yields per-file progress events.

Mirrors :func:`fnd.index.build_index_from_config` but cooperative:
each per-file unit of work runs in ``asyncio.to_thread`` so the event
loop stays responsive for the TUI / CLI progress display.

Progress events stream through an async generator. The caller (CLI
``rich.progress`` bar or TUI modal) consumes them and updates the
display. A ``cancel`` :class:`asyncio.Event` lets the caller request
a clean stop at the next file boundary.

State persistence — atomic writes after each file completion to
``$XDG_DATA_HOME/fnd/reindex/<collection>.state.toml`` — means a
kill / quit / power-loss can be resumed simply by re-running:
- Cache hits skip already-extracted files
- State file tells us how far we got, for progress display on resume
- A clean completion deletes the state file (so next launch doesn't
  show a stale "resume?" prompt)
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import sys
import tempfile
import time
import tomllib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import tomli_w
from platformdirs import user_data_dir

from fnd.cache import ExtractionCache
from fnd.config import CollectionConfig
from fnd.extract import ExtractError, extract
from fnd.frontmatter import FrontmatterParseError, read_frontmatter_from_file
from fnd.index import (
    _COMMIT_BATCH,
    _WRITER_HEAP,
    _doc_for_chunk,
    _ensure_index,
    _path_parent_id,
)
from fnd.meta_blob import encode as encode_meta_blob
from fnd.schema import F_COLLECTION, F_PARENT_ID
from fnd.walk import is_dataless, walk_sources

EventKind = Literal[
    "started",
    "file_processing",
    "file_complete",
    "file_error",
    "done",
    "cancelled",
]


@dataclass(slots=True)
class ProgressEvent:
    kind: EventKind
    files_done: int = 0
    files_total: int = 0
    pdfs_total: int = 0
    current_file: str = ""
    file_elapsed_ms: float = 0.0
    cache_hit: bool = False
    cache_hits_total: int = 0
    cache_misses_total: int = 0
    elapsed_s: float = 0.0
    error: str = ""
    # Per-file classification (set on file_complete / file_error).
    is_pdf: bool = False
    is_dataless: bool = False
    has_textured_chunk: bool = False
    # Running totals across the run; modal renders the Indexed +
    # Texturising lines from these.
    indexed_newly_total: int = 0
    indexed_already_total: int = 0
    textured_newly_total: int = 0
    textured_already_total: int = 0
    still_flat_total: int = 0
    failed_total: int = 0


@dataclass(slots=True)
class IndexState:
    """On-disk record of an in-flight reindex."""

    collection: str
    started_at: str
    total_files: int
    pdfs_total: int = 0
    files_completed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    indexed_newly: int = 0
    indexed_already: int = 0
    textured_newly: int = 0
    textured_already: int = 0
    still_flat: int = 0
    failed: int = 0
    current_file: str = ""
    last_update: str = ""

    def to_toml_dict(self) -> dict[str, object]:
        return {
            "state": {
                "collection": self.collection,
                "started_at": self.started_at,
                "total_files": self.total_files,
                "pdfs_total": self.pdfs_total,
                "files_completed": self.files_completed,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "indexed_newly": self.indexed_newly,
                "indexed_already": self.indexed_already,
                "textured_newly": self.textured_newly,
                "textured_already": self.textured_already,
                "still_flat": self.still_flat,
                "failed": self.failed,
                "current_file": self.current_file,
                "last_update": self.last_update,
            }
        }

    @classmethod
    def from_toml_dict(cls, data: dict[str, object]) -> IndexState:
        raw = data.get("state", {})
        if not isinstance(raw, dict):
            raise ValueError("malformed state file (missing [state] table)")
        s = cast(dict[str, Any], raw)
        return cls(
            collection=str(s.get("collection", "")),
            started_at=str(s.get("started_at", "")),
            total_files=int(s.get("total_files", 0) or 0),
            pdfs_total=int(s.get("pdfs_total", 0) or 0),
            files_completed=int(s.get("files_completed", 0) or 0),
            cache_hits=int(s.get("cache_hits", 0) or 0),
            cache_misses=int(s.get("cache_misses", 0) or 0),
            indexed_newly=int(s.get("indexed_newly", 0) or 0),
            indexed_already=int(s.get("indexed_already", 0) or 0),
            textured_newly=int(s.get("textured_newly", 0) or 0),
            textured_already=int(s.get("textured_already", 0) or 0),
            still_flat=int(s.get("still_flat", 0) or 0),
            failed=int(s.get("failed", 0) or 0),
            current_file=str(s.get("current_file", "")),
            last_update=str(s.get("last_update", "")),
        )


def state_file_for(collection: str) -> Path:
    """Where the in-flight state for ``collection`` lives."""
    return Path(user_data_dir("fnd")) / "reindex" / f"{collection}.state.toml"


def load_state(state_path: Path) -> IndexState | None:
    """Read state from disk, or None if the file is absent / corrupt."""
    if not state_path.exists():
        return None
    try:
        with state_path.open("rb") as f:
            data = tomllib.load(f)
        return IndexState.from_toml_dict(data)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return None


def save_state(state_path: Path, state: IndexState) -> None:
    """Atomic write of state to disk; safe to call after every file."""
    state.last_update = dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(state.to_toml_dict(), f)
        os.replace(tmp, state_path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def clear_state(state_path: Path) -> None:
    """Remove the state file (called on clean completion / discard)."""
    with contextlib.suppress(OSError):
        state_path.unlink()


def _enumerate_paths(config: CollectionConfig) -> list[tuple[Path, str]]:
    """Eagerly walk all sources to count files for ETA + total bar.

    Returns ``(path, source_id)`` pairs in deterministic walk order.
    """
    out: list[tuple[Path, str]] = []
    for source in config.sources:
        source_id = str(Path(source.path).expanduser().resolve())
        for path in walk_sources(sources=[source]):
            out.append((path, source_id))
    return out


def _process_one_file(
    *,
    path: Path,
    source_id: str,
    collection: str,
    writer: Any,  # tantivy IndexWriter (no public type stub)
    cache_before_hits: int,
    cache: ExtractionCache,
) -> tuple[int, bool, bool, str]:
    """Synchronous per-file work — extraction + write to Tantivy.

    Returns ``(chunks_written, cache_hit, has_textured_chunk, error_msg)``.
    ``has_textured_chunk`` is True iff any emitted chunk carries a non-empty
    ``body_md`` (PDFs that hit the structured pipeline). Run inside
    ``asyncio.to_thread`` so the caller's event loop stays responsive.
    """
    # iCloud-offloaded placeholder: skip rather than triggering a sync
    # download that could blow the worker's stall budget.
    if is_dataless(path):
        return 0, False, False, "iCloud-offloaded - download in Finder before indexing"

    meta_blob_bytes = b""
    if path.suffix.lower() == ".md":
        try:
            fm = read_frontmatter_from_file(path)
        except FrontmatterParseError:
            fm = None
        if fm:
            meta_blob_bytes = encode_meta_blob(fm)

    parent_id = _path_parent_id(path)
    writer.delete_documents(F_PARENT_ID, parent_id)
    n_chunks = 0
    has_textured = False
    try:
        for chunk in extract(path):
            if chunk.body_md:
                has_textured = True
            writer.add_document(
                _doc_for_chunk(
                    chunk,
                    collection=collection,
                    source_path=source_id,
                    meta_blob_bytes=meta_blob_bytes,
                )
            )
            n_chunks += 1
    except ExtractError as e:
        writer.delete_documents(F_PARENT_ID, parent_id)
        return n_chunks, False, False, str(e)

    hit = cache.hits > cache_before_hits
    return n_chunks, hit, has_textured, ""


async def run_indexer(
    *,
    config: CollectionConfig,
    collection: str,
    index_dir: Path,
    rebuild: bool = False,
    state_path: Path | None = None,
    cancel: asyncio.Event | None = None,
) -> AsyncIterator[ProgressEvent]:
    """Async generator yielding ProgressEvents as the index builds.

    Drop-in replacement for ``build_index_from_config`` when called
    from an async context (TUI). The CLI's existing sync entrypoint
    keeps working via ``run_sync()`` below.
    """
    state_path = state_path or state_file_for(collection)

    # Cache instance whose counters the runner can read for hit/miss
    # stats per file. Swap the production singleton briefly so
    # ``extract()`` uses it; restore on exit.
    cache = ExtractionCache()
    from fnd.extract import pdf as _pdf

    prior_singleton = _pdf._cache_singleton
    _pdf._cache_singleton = cache

    # Run-scoped "Update cache at index time" toggle. When the user has
    # turned this off (battery-saver), extract() skips fresh structured
    # extraction on cache misses and skips cache writes — see
    # fnd/extract/pdf.py::set_skip_structure_extraction.
    skip_structure = False
    with contextlib.suppress(Exception):
        from fnd.config import load as _load_config

        full_cfg = _load_config()
        skip_structure = not bool(full_cfg.defaults.cache_at_index_time)
    prior_skip = _pdf._skip_structure_extraction
    _pdf.set_skip_structure_extraction(skip_structure)

    started_at = dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
    t_start = time.perf_counter()
    paths = _enumerate_paths(config)
    pdfs_total = sum(1 for p, _src in paths if p.suffix.lower() == ".pdf")
    state = IndexState(
        collection=collection,
        started_at=started_at,
        total_files=len(paths),
        pdfs_total=pdfs_total,
    )
    save_state(state_path, state)

    def _emit(kind: EventKind, **extra: Any) -> ProgressEvent:
        return ProgressEvent(
            kind=kind,
            files_done=state.files_completed,
            files_total=len(paths),
            pdfs_total=pdfs_total,
            cache_hits_total=cache.hits,
            cache_misses_total=cache.misses,
            indexed_newly_total=state.indexed_newly,
            indexed_already_total=state.indexed_already,
            textured_newly_total=state.textured_newly,
            textured_already_total=state.textured_already,
            still_flat_total=state.still_flat,
            failed_total=state.failed,
            elapsed_s=time.perf_counter() - t_start,
            **extra,
        )

    yield _emit("started")

    try:
        index = _ensure_index(index_dir, force=rebuild)
        writer = index.writer(heap_size=_WRITER_HEAP)
        if rebuild:
            writer.delete_documents(F_COLLECTION, collection)
            writer.commit()

        written = 0
        for path, source_id in paths:
            if cancel is not None and cancel.is_set():
                yield _emit("cancelled")
                # Leave state file in place so we can resume.
                return

            is_pdf = path.suffix.lower() == ".pdf"
            state.current_file = str(path)
            yield _emit("file_processing", current_file=str(path), is_pdf=is_pdf)

            hits_before = cache.hits
            t_file = time.perf_counter()
            chunks_written, was_hit, has_textured, err = await asyncio.to_thread(
                _process_one_file,
                path=path,
                source_id=source_id,
                collection=collection,
                writer=writer,
                cache_before_hits=hits_before,
                cache=cache,
            )
            file_elapsed_ms = (time.perf_counter() - t_file) * 1000.0
            was_dataless = err.startswith("iCloud-offloaded") if err else False

            if err:
                state.failed += 1
            else:
                if is_pdf:
                    if was_hit:
                        state.textured_already += 1
                        state.indexed_already += 1
                    elif has_textured:
                        state.textured_newly += 1
                        state.indexed_newly += 1
                    else:
                        state.still_flat += 1
                        state.indexed_newly += 1
                else:
                    if was_hit:
                        state.indexed_already += 1
                    else:
                        state.indexed_newly += 1

            written += chunks_written
            state.files_completed += 1
            state.cache_hits = cache.hits
            state.cache_misses = cache.misses
            # Atomic state update per file = resume granularity per file.
            save_state(state_path, state)

            if err:
                print(f"[fnd skip] {err}", file=sys.stderr)
                yield _emit(
                    "file_error",
                    current_file=str(path),
                    file_elapsed_ms=file_elapsed_ms,
                    error=err,
                    is_pdf=is_pdf,
                    is_dataless=was_dataless,
                )

            if written % _COMMIT_BATCH == 0 and written > 0:
                writer.commit()

            yield _emit(
                "file_complete",
                current_file=str(path),
                file_elapsed_ms=file_elapsed_ms,
                cache_hit=was_hit,
                is_pdf=is_pdf,
                has_textured_chunk=has_textured,
                is_dataless=was_dataless,
            )

        writer.commit()
        writer.wait_merging_threads()
    finally:
        _pdf._cache_singleton = prior_singleton
        _pdf.set_skip_structure_extraction(prior_skip)

    # Clean completion. Wipe the state file so next launch doesn't
    # show a stale "resume?" prompt.
    clear_state(state_path)
    final_elapsed = time.perf_counter() - t_start

    # Persist throughput so future ETAs calibrate to this machine's
    # actual speed. PDF count drives the per-PDF figure; runs of <3
    # PDFs are dropped inside ``record_run`` because tiny runs are
    # dominated by setup cost.
    with contextlib.suppress(Exception):
        from fnd.tui.cost_estimate import record_run

        record_run(
            n_pdfs=pdfs_total,
            cache_hits=cache.hits,
            cache_misses=cache.misses,
            elapsed_s=final_elapsed,
        )

    yield _emit("done")


def run_sync(
    *,
    config: CollectionConfig,
    collection: str,
    index_dir: Path,
    rebuild: bool = False,
    progress_callback: Callable[[ProgressEvent], None] | None = None,
) -> int:
    """Sync wrapper for CLI use — drives the async runner to completion.

    Drives the async iterator with ``asyncio.run`` and surfaces each
    progress event to ``progress_callback(event)``. Returns the
    number of files processed.
    """

    async def _drive() -> int:
        n = 0
        async for ev in run_indexer(
            config=config,
            collection=collection,
            index_dir=index_dir,
            rebuild=rebuild,
        ):
            if progress_callback is not None:
                progress_callback(ev)
            if ev.kind == "done":
                n = ev.files_done
        return n

    return asyncio.run(_drive())


__all__ = [
    "IndexState",
    "ProgressEvent",
    "clear_state",
    "load_state",
    "run_indexer",
    "run_sync",
    "save_state",
    "state_file_for",
]
