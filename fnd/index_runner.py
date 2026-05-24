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
from fnd.schema import F_COLLECTION
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
    # Size-weighted progress. ETA derived from file count alone treats
    # a 12MB AWS PDF the same as a 4KB note, so the ETA snaps when a
    # heavy file finishes. bytes_done/bytes_total tracks the same
    # progression weighted by file size - a much better proxy for
    # remaining work than file count.
    bytes_done: int = 0
    bytes_total: int = 0
    # Cache-miss-only counters for the ETA rate. Cache hits cost ~ms
    # but bump bytes_done; if the ETA used the overall rate it would
    # read ~0 for a cache-heavy run with a slow fresh PDF still in
    # the queue. Tracking extract-only bytes + extract-only seconds
    # gives an honest per-byte extraction rate that survives mixed
    # workloads.
    extract_bytes_done: int = 0
    extract_seconds_spent: float = 0.0
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
    schema: Any,  # tantivy Schema (needed for delete-by-query)
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

    is_pdf = path.suffix.lower() == ".pdf"
    # Non-PDFs don't use the structured-extraction cache (their
    # extraction is already cheap), so cache.hits never increments
    # for them and they'd otherwise always count as "newly indexed"
    # on every launch. The seen log gives them a comparable
    # "have we processed this content before?" signal so a stable
    # md corpus reports "already indexed" on re-runs.
    non_pdf_sha = ""
    non_pdf_was_seen = False
    if not is_pdf:
        from fnd.cache import sha256_file
        from fnd.seen_log import has_seen

        try:
            non_pdf_sha = sha256_file(path)
            non_pdf_was_seen = has_seen(non_pdf_sha)
        except OSError:
            non_pdf_sha = ""

    parent_id = _path_parent_id(path)
    # Delete only THIS collection's chunks for this file. The previous
    # unscoped delete_documents(F_PARENT_ID, ...) was a per-path nuke
    # that wiped sibling collections' chunks too when a file was
    # shared (typical case: an Obsidian Vault listed under multiple
    # collections' sources). See fnd.index._scoped_delete_query.
    from fnd.index import _scoped_delete_query

    _delete_q = _scoped_delete_query(schema, collection, parent_id)
    writer.delete_documents_by_query(_delete_q)
    n_chunks = 0
    has_textured = False
    # Per-page beats from the PDF worker feed the live-progress
    # channel so the modal's 1Hz ETA can refine while a long PDF is
    # mid-extraction (instead of waiting for the file_complete event).
    from fnd.tui.live_progress import report_heartbeat as _report_heartbeat

    try:
        for chunk in extract(path, on_heartbeat=_report_heartbeat):
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
        # Same collection-scoped delete as above: never touch sibling
        # collections' chunks on an extraction error.
        writer.delete_documents_by_query(_delete_q)
        return n_chunks, False, False, str(e)

    if not is_pdf and non_pdf_sha:
        from fnd.seen_log import mark_seen

        mark_seen(non_pdf_sha)

    hit = (cache.hits > cache_before_hits) if is_pdf else non_pdf_was_seen
    return n_chunks, hit, has_textured, ""


async def run_indexer(
    *,
    config: CollectionConfig,
    collection: str,
    index_dir: Path,
    rebuild: bool = False,
    state_path: Path | None = None,
    cancel: asyncio.Event | None = None,
    texturise_override: bool | None = None,
) -> AsyncIterator[ProgressEvent]:
    """Async generator yielding ProgressEvents as the index builds.

    Drop-in replacement for ``build_index_from_config`` when called
    from an async context (TUI). The CLI's existing sync entrypoint
    keeps working via ``run_sync()`` below.

    ``texturise_override`` lets the caller override the "Texturise PDFs
    while indexing" toggle for this run only:
    - ``None`` (default) follows the toggle
    - ``True`` forces texturising on regardless of the toggle (the
      shared "Update everything (index + texturise)" action)
    - ``False`` forces texturising off regardless of the toggle (the
      "Process new files (index only)" action)
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
    # fnd/extract/pdf.py::set_skip_structure_extraction. An explicit
    # texturise_override from the caller wins over the toggle.
    if texturise_override is True:
        skip_structure = False
    elif texturise_override is False:
        skip_structure = True
    else:
        skip_structure = False
        with contextlib.suppress(Exception):
            from fnd.config import load as _load_config

            full_cfg = _load_config()
            skip_structure = not bool(full_cfg.defaults.cache_at_index_time)
    prior_skip = _pdf._skip_structure_extraction
    _pdf.set_skip_structure_extraction(skip_structure)

    # Clear any cancel beacon left over from a previous Cancel click
    # so a fresh run isn't aborted by stale state.
    with contextlib.suppress(Exception):
        from fnd.extract._worker import clear_cancel as _clear_cancel

        _clear_cancel()

    started_at = dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
    t_start = time.perf_counter()
    paths = _enumerate_paths(config)
    pdfs_total = sum(1 for p, _src in paths if p.suffix.lower() == ".pdf")
    # Per-file byte sizes for the size-weighted ETA. A non-existent /
    # unreadable file contributes 0 and is counted as 0-byte done at
    # completion - same outcome as the file-count fallback for that
    # entry without distorting the rate for the rest.
    sizes: dict[str, int] = {}
    for p, _src in paths:
        try:
            sizes[str(p)] = p.stat().st_size
        except OSError:
            sizes[str(p)] = 0
    bytes_total = sum(sizes.values())
    bytes_done_state = [0]  # mutable closure-cell so _emit reads latest
    # Cache-miss-only counters: bytes processed via real extraction
    # and the seconds we spent doing it. Drives the ETA rate so a
    # cache-heavy chain doesn't collapse ETA to 0 just because the
    # cache hits ramped bytes_done while contributing ~0 time.
    extract_bytes_state = [0]
    extract_seconds_state = [0.0]

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
            bytes_done=bytes_done_state[0],
            bytes_total=bytes_total,
            extract_bytes_done=extract_bytes_state[0],
            extract_seconds_spent=extract_seconds_state[0],
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
                # Save everything we processed before cancel fired so
                # the user doesn't lose those files on the next launch.
                with contextlib.suppress(Exception):
                    writer.commit()
                yield _emit("cancelled")
                # Leave state file in place so we can resume.
                return
            # Reset the worker-level cancel beacon at file boundary.
            # A previous Skip / Cancel set it to bypass the retry for
            # the file that just failed; if we left it set, this new
            # file would silently bypass its own legitimate retry on a
            # transient BrokenProcessPool / StallError and surface as
            # a spurious failure.
            with contextlib.suppress(Exception):
                from fnd.extract._worker import clear_cancel as _clear_cancel

                _clear_cancel()

            is_pdf = path.suffix.lower() == ".pdf"
            state.current_file = str(path)
            # Clear the live-progress snapshot before the new file so a
            # stale heartbeat from the previous extraction can't bleed
            # into this file's ETA.
            with contextlib.suppress(Exception):
                from fnd.tui.live_progress import reset as _live_reset

                _live_reset()
            yield _emit("file_processing", current_file=str(path), is_pdf=is_pdf)

            hits_before = cache.hits
            t_file = time.perf_counter()
            chunks_written, was_hit, has_textured, err = await asyncio.to_thread(
                _process_one_file,
                path=path,
                source_id=source_id,
                collection=collection,
                writer=writer,
                schema=index.schema,
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
                        # Cache hits can be either textured or flat -
                        # the cache stores whatever the original
                        # extraction produced. Older entries (or runs
                        # where docling wasn't installed yet) may have
                        # empty body_md, so a hit doesn't imply
                        # texturised. Checking has_textured here means
                        # the settings status "N still flat" matches
                        # the indexer's own count instead of drifting.
                        state.indexed_already += 1
                        if has_textured:
                            state.textured_already += 1
                        else:
                            state.still_flat += 1
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
            # Whether the file succeeded or failed, the time we spent on
            # it is "used up" - count its bytes toward the size-weighted
            # progress so the ETA reflects elapsed-vs-remaining bytes,
            # not just elapsed-vs-remaining file count.
            file_size = sizes.get(str(path), 0)
            bytes_done_state[0] += file_size
            # Track extract-only metrics for the ETA rate. Cache hits
            # cost ~ms but bump bytes_done; if the ETA used the
            # overall rate the cache-hit barrage at chain start would
            # crush the per-byte rate and ETA would read 0 even with
            # a multi-minute uncached PDF still pending. Only the
            # actual extraction work feeds the rate.
            if not was_hit:
                extract_bytes_state[0] += file_size
                extract_seconds_state[0] += file_elapsed_ms / 1000.0
            # Atomic state update per file = resume granularity per file.
            save_state(state_path, state)

            if err:
                # Enrich the error with the page where the wedge happened
                # so the still-flat drill-in shows "stuck at page 15/23"
                # not just "extractor wedged". The live-progress snapshot
                # is the last page beat the worker emitted before dying.
                if is_pdf:
                    with contextlib.suppress(Exception):
                        from fnd.tui.live_progress import snapshot as _lp_snapshot

                        _p, _done, _total, _start = _lp_snapshot()
                        if _total > 0 and _done > 0 and _done < _total:
                            err = f"{err}  [last page beat: {_done}/{_total}]"
                ts = dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
                print(f"[fnd skip {ts}] {err}", file=sys.stderr)
                # Persist the failure so the still-flat drill-in screen
                # can show per-file reasons + retry buttons.
                with contextlib.suppress(Exception):
                    from fnd.tui.failure_log import record_failure

                    record_failure(collection=collection, path=str(path), reason=err)
                yield _emit(
                    "file_error",
                    current_file=str(path),
                    file_elapsed_ms=file_elapsed_ms,
                    error=err,
                    is_pdf=is_pdf,
                    is_dataless=was_dataless,
                )
            else:
                # A previously-failed file just succeeded; clear the
                # stale record so the drill-in stops listing it.
                with contextlib.suppress(Exception):
                    from fnd.tui.failure_log import clear_failure

                    clear_failure(collection=collection, path=str(path))

            # Commit on EITHER cadence: every 500 chunks (the existing
            # batch boundary) OR every 10 files. Without the per-file
            # ceiling, a chain that hangs on file 25 would lose the
            # first 24 if their chunks didn't add up to 500; the user
            # would re-extract them on next launch.
            commit_due = (written % _COMMIT_BATCH == 0 and written > 0) or (
                state.files_completed % 10 == 0
            )
            if commit_due:
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

    # Cancel-during-last-file: the top-of-loop check at line ~408
    # only fires on the NEXT iteration, so a Cancel pressed while the
    # final file is in flight would otherwise emit "done" and look to
    # the user as if Cancel was ignored. Honour the flag here so the
    # modal sees the truthful "cancelled" terminal event.
    if cancel is not None and cancel.is_set():
        yield _emit("cancelled")
        return
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
