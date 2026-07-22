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
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

import tomli_w

from fnd import paths
from fnd.cache import ExtractionCache
from fnd.config import CollectionConfig
from fnd.extract import ExtractError, extract
from fnd.fsmeta import read_file_times
from fnd.index import (
    _COMMIT_BATCH,
    _WRITER_HEAP,
    _doc_for_chunk,
    _ensure_index,
    _path_parent_id,
    read_file_metadata,
)
from fnd.schema import F_COLLECTION
from fnd.walk import is_dataless, walk_sources

EventKind = Literal[
    "enumerating",
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
    return paths.reindex_state_path(collection)


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


# A saved state older than this is treated as abandoned, not resumable: a
# day-plus-old "in-flight" index is almost always a stale file (a past crash
# the user already re-ran by hand, or leaked test state) rather than a run
# worth silently restarting heavy work for.
RESUME_MAX_AGE_HOURS: Final[float] = 24.0


def is_state_resumable(
    state: IndexState | None,
    *,
    known_collections: Collection[str],
    now: dt.datetime,
    max_age_hours: float = RESUME_MAX_AGE_HOURS,
) -> bool:
    """Whether a saved index state should be auto-resumed on launch.

    Guards against silently restarting heavy indexing off state that isn't
    really the user's own just-interrupted run: requires unfinished work, a
    collection that still exists in the current config, and a recent
    timestamp. Foreign/leaked state (e.g. a test writing to the real data
    dir) and long-abandoned crashes are rejected.
    """
    if state is None or state.total_files <= 0:
        return False
    if state.files_completed >= state.total_files:
        return False
    if state.collection not in known_collections:
        return False
    stamp = state.last_update or state.started_at
    try:
        ts = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    age_hours = (now - ts).total_seconds() / 3600.0
    return 0 <= age_hours <= max_age_hours


def _enumerate_paths(config: CollectionConfig) -> list[tuple[Path, str]]:
    """Eagerly walk all sources to count files for ETA + total bar.

    Returns ``(path, source_id)`` pairs in deterministic walk order.
    Honours ``defaults.skip_junk_dirs`` + ``extra_junk_dirs`` so dev
    trees (``node_modules``, ``.venv``, …) are pruned at descent.
    """
    from fnd.config import load as _load_config
    from fnd.walk import resolve_skip_dirs

    try:
        skip = resolve_skip_dirs(_load_config().defaults)
    except Exception:
        skip = resolve_skip_dirs(None)
    out: list[tuple[Path, str]] = []
    for source in config.sources:
        try:
            source_id = str(Path(source.path).expanduser().resolve())
        except OSError:
            source_id = str(Path(source.path).expanduser())
        for path in walk_sources(sources=[source], skip_dirs=skip):
            out.append((path, source_id))
    return out


def _should_reprocess(
    *, prior_mtime: int, prior_ctime: int, cur_mtime: int, cur_ctime: int
) -> bool:
    """Whether a file already in the index needs re-processing.

    mtime catches content edits. ctime additionally catches metadata-only
    edits — a Finder retag moves ctime alone, so an mtime-only check would
    leave tags stale until the file's content happened to change.

    ``prior_ctime`` of 0 means the stored doc predates the field (a v7 index
    read during migration): treat it as "no information" rather than as a
    change, so upgrading doesn't re-extract the entire corpus.
    """
    if cur_mtime != prior_mtime:
        return True
    return bool(prior_ctime) and cur_ctime != prior_ctime


def _prior_indexed_state(
    searcher: Any, schema: Any, collection: str, parent_id: str
) -> tuple[int | None, int, bool]:
    """``(stored_mtime, stored_inode_ctime, has_textured)`` for this file's
    prior-committed chunks in this collection, or ``(None, 0, False)`` if absent.

    All of a file's chunks share both timestamps, so the first hit settles
    them; ``has_textured`` scans a handful of chunks for a non-empty
    ``body_md``. Used by the incremental skip to decide whether an unchanged
    file needs any work this run.
    """
    from fnd.index import _scoped_delete_query
    from fnd.schema import F_BODY_MD, F_INODE_CTIME, F_MTIME

    try:
        result = searcher.search(_scoped_delete_query(schema, collection, parent_id), limit=16)
    except Exception:
        return None, 0, False
    mtime: int | None = None
    inode_ctime = 0
    has_textured = False
    for _score, addr in result.hits:
        doc = searcher.doc(addr)
        if mtime is None:
            mv = doc.get_first(F_MTIME)  # type: ignore[attr-defined]
            if mv is not None:
                mtime = int(mv)
            # Absent on v7 docs read mid-migration; 0 means "no information".
            cv = doc.get_first(F_INODE_CTIME)  # type: ignore[attr-defined]
            if cv is not None:
                inode_ctime = int(cv)
        if doc.get_first(F_BODY_MD):  # type: ignore[attr-defined]
            has_textured = True
    return mtime, inode_ctime, has_textured


def _process_one_file(
    *,
    path: Path,
    source_id: str,
    collection: str,
    writer: Any,  # tantivy IndexWriter (no public type stub)
    schema: Any,  # tantivy Schema (needed for delete-by-query)
    cache_before_hits: int,
    cache: ExtractionCache,
    prior_searcher: Any = None,
    skip_unchanged: bool = False,
    texturise_on: bool = True,
    wipe: bool = False,
    tag_sources: Sequence[str] = ("frontmatter", "os"),
    tag_frontmatter_keys: Sequence[str] = (),
) -> tuple[int, bool, bool, str]:
    """Synchronous per-file work — extraction + write to Tantivy.

    Returns ``(chunks_written, cache_hit, has_textured_chunk, error_msg)``.
    ``has_textured_chunk`` is True iff any emitted chunk carries a non-empty
    ``body_md`` (PDFs that hit the structured pipeline). Run inside
    ``asyncio.to_thread`` so the caller's event loop stays responsive.

    When ``skip_unchanged`` and ``prior_searcher`` is given, a file already
    in this collection's committed index with an unchanged mtime AND ctime
    is skipped entirely (no delete, no extraction) — returned as a cache
    hit so it counts as already-indexed. ctime is checked because a Finder
    retag moves it without touching mtime. The one exception is a flat PDF
    we could newly texturise this run (``texturise_on`` and no prior
    ``body_md``).
    """
    # iCloud-offloaded placeholder: skip rather than triggering a sync
    # download that could blow the worker's stall budget.
    if is_dataless(path):
        return 0, False, False, "iCloud-offloaded - download in Finder before indexing"

    is_pdf = path.suffix.lower() == ".pdf"
    parent_id = _path_parent_id(path)

    # Incremental skip: an unchanged file already in this collection's
    # committed index needs no work this run.
    if skip_unchanged and prior_searcher is not None:
        prior_mtime, prior_ctime, prior_textured = _prior_indexed_state(
            prior_searcher, schema, collection, parent_id
        )
        if prior_mtime is not None:
            times = read_file_times(path)
            # read_file_times zeroes on stat failure; fall back to the stored
            # values so a transient error reads as "unchanged", not "changed".
            cur_mtime = times.mtime or prior_mtime
            cur_ctime = times.inode_changed or prior_ctime
            # Re-process only if changed, or if it's a flat PDF this run
            # could texturise — the one improvement an incremental pass
            # should still make.
            improvable = is_pdf and texturise_on and not prior_textured
            changed = _should_reprocess(
                prior_mtime=prior_mtime,
                prior_ctime=prior_ctime,
                cur_mtime=cur_mtime,
                cur_ctime=cur_ctime,
            )
            if not changed and not improvable:
                return 0, True, prior_textured, ""

    # Read once per file, stamped onto every chunk. Shared with build_index so
    # an ad-hoc `fnd index <root>` captures the same metadata as a reindex.
    meta_blob_bytes, file_tags = read_file_metadata(
        path, tag_sources=tag_sources, frontmatter_keys=tag_frontmatter_keys
    )

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
        from fnd.seen_log import forget as seen_forget
        from fnd.seen_log import has_seen

        try:
            non_pdf_sha = sha256_file(path)
            if wipe:
                # Literal Rebuild: drop the seen-marker so this genuinely
                # re-indexed file reports as newly indexed, not "already".
                seen_forget(non_pdf_sha)
                non_pdf_was_seen = False
            else:
                non_pdf_was_seen = has_seen(non_pdf_sha)
        except OSError:
            non_pdf_sha = ""

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
                    tags=file_tags,
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
    skip_unchanged: bool = True,
    force_fresh: bool = False,
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

    ``skip_unchanged`` (default True) enables the incremental skip: a file
    already in this collection's committed index with an unchanged mtime and
    ctime is left untouched. The "Re-texturise outdated" action passes False so it
    can revisit unchanged files. ``force_fresh`` (default False) is the
    "Re-texturise outdated" opt-out from durable cache reuse — when True,
    ``extract()`` only reuses a current-signature entry and otherwise
    re-extracts fresh, upgrading pre-version texturising.
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
    texturise_on = not skip_structure

    # Tag settings come from the user's config, sourced here (the same
    # internal-load pattern used for cache_at_index_time above) so every
    # indexing path — including the TUI "Update index" modal that drives
    # this runner — honours tag_sources / tag_frontmatter_keys rather than
    # the bare defaults. Missing config falls back to the defaults.
    tag_sources: Sequence[str] = ("frontmatter", "os")
    tag_frontmatter_keys: Sequence[str] = ()
    with contextlib.suppress(Exception):
        from fnd.config import load as _load_config

        _defaults = _load_config().defaults
        tag_sources = tuple(_defaults.tag_sources)
        tag_frontmatter_keys = tuple(_defaults.tag_frontmatter_keys)

    # Re-texturise-outdated opt-out from durable reuse (see extract()).
    prior_force_fresh = _pdf._force_fresh_texture
    _pdf.set_force_fresh_texture(force_fresh)

    # Clear any cancel beacon left over from a previous Cancel click
    # so a fresh run isn't aborted by stale state.
    with contextlib.suppress(Exception):
        from fnd.extract._worker import clear_cancel as _clear_cancel

        _clear_cancel()

    started_at = dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
    t_start = time.perf_counter()
    # Mutable closure cells for _emit so an early ``enumerating`` event
    # carries zeros while the walk is still pending.
    paths: list[tuple[Path, str]] = []
    sizes: dict[str, int] = {}
    pdfs_total = 0
    bytes_total = 0
    bytes_done_state = [0]
    # Cache-miss-only counters: bytes processed via real extraction
    # and the seconds we spent doing it. Drives the ETA rate so a
    # cache-heavy chain doesn't collapse ETA to 0 just because the
    # cache hits ramped bytes_done while contributing ~0 time.
    extract_bytes_state = [0]
    extract_seconds_state = [0.0]

    state = IndexState(
        collection=collection,
        started_at=started_at,
        total_files=0,
        pdfs_total=0,
    )

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

    # Surface an immediate "enumerating" event so the modal mounts and
    # shows "Scanning sources…" while the synchronous filesystem walk
    # runs on a worker thread. Without this hop the event loop stays
    # blocked in scandir / stat / tantivy IO and the UI looks frozen.
    yield _emit("enumerating")

    def _prepare() -> tuple[list[tuple[Path, str]], dict[str, int], int, int, Any, Any, Any]:
        local_paths = _enumerate_paths(config)
        local_sizes: dict[str, int] = {}
        for p, _src in local_paths:
            try:
                local_sizes[str(p)] = p.stat().st_size
            except OSError:
                local_sizes[str(p)] = 0
        local_pdfs_total = sum(1 for p, _src in local_paths if p.suffix.lower() == ".pdf")
        local_bytes_total = sum(local_sizes.values())
        local_index = _ensure_index(index_dir, force=rebuild)
        local_writer = local_index.writer(heap_size=_WRITER_HEAP)
        if rebuild:
            local_writer.delete_documents(F_COLLECTION, collection)
            local_writer.commit()
        # Prior-committed snapshot for the incremental skip. A point-in-time
        # searcher reflects only what previous runs committed; files we
        # process this run are deleted+re-added, never skipped later, so the
        # start-of-run snapshot is the correct "already indexed?" oracle.
        local_prior_searcher = None
        if skip_unchanged and not rebuild:
            with contextlib.suppress(Exception):
                local_prior_searcher = local_index.searcher()
        return (
            local_paths,
            local_sizes,
            local_pdfs_total,
            local_bytes_total,
            local_index,
            local_writer,
            local_prior_searcher,
        )

    try:
        (
            paths,
            sizes,
            pdfs_total,
            bytes_total,
            index,
            writer,
            prior_searcher,
        ) = await asyncio.to_thread(_prepare)
    except Exception as e:
        # Without this backstop a LockBusy (concurrent indexer on the
        # same index_dir) or any other _prepare failure would kill the
        # drive_indexer task silently and leave the IndexerScreen frozen
        # at "Scanning sources…" forever. Emit a file_error so the
        # modal shows the reason, then a terminal cancelled so it
        # transitions out of the enumerating state and the user can
        # close it.
        yield _emit(
            "file_error",
            current_file="(setup)",
            error=f"Could not start indexer: {e}",
        )
        yield _emit("cancelled")
        return
    state.total_files = len(paths)
    state.pdfs_total = pdfs_total
    save_state(state_path, state)
    yield _emit("started")

    try:
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
                prior_searcher=prior_searcher,
                skip_unchanged=skip_unchanged,
                texturise_on=texturise_on,
                wipe=force_fresh,
                tag_sources=tag_sources,
                tag_frontmatter_keys=tag_frontmatter_keys,
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
        _pdf.set_force_fresh_texture(prior_force_fresh)

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
    "is_state_resumable",
    "load_state",
    "run_indexer",
    "run_sync",
    "save_state",
    "state_file_for",
]
