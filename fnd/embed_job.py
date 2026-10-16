"""The one embedding job: bring the vector store and its row table in line with the index.

:func:`reconcile` is idempotent: it reads every section once, embeds whole files whose
sections lack a vector, and saves the row table as it goes, so stopping at any point loses
only the batch in flight and the next run picks up the rest. Vectors are never collected
while a reindex is unfinished, since a rebuild wipes the index before refilling it.

The bulk run happens in its own process (``python -m fnd.embed_job``), niced, with its
threads capped, because onnxruntime keeps the memory it peaks at; :class:`EmbedJobRunner`
drives that process from the TUI.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import importlib
import json
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from fnd.semantic import GRANITE, Embedder, section_key, section_text
from fnd.vectors import stored_digest

if TYPE_CHECKING:
    from fnd.query import Hit, Searcher
    from fnd.vectors import RowTable, VectorStore

__all__ = ["CANCELLED_EXIT", "EmbedJobRunner", "Progress", "indexed_sections", "reconcile"]

BATCH: Final = 16
# Batches per segment file: one per batch made ~6,800 files for 109k sections.
FLUSH_EVERY: Final = 8
# Seconds between row-table saves while running: per-batch saves wrote ~14 GB in a first embed.
SAVE_EVERY_S: Final = 60.0
NICE: Final = 10
CANCELLED_EXIT: Final = 3
# Another process holds the store: the CLI's reconcile, or a second app.
BUSY_EXIT: Final = 4
_NEVER: Final = ("", 0, 0)


@dataclass(frozen=True, slots=True)
class Progress:
    done: int
    total: int


def indexed_sections(searcher: Searcher) -> list[Hit]:
    """Each indexed section once, carrying every collection it belongs to."""
    import tantivy

    from fnd.query import _light_hit

    raw: Any = searcher._searcher
    query = tantivy.Query.all_query()
    total = raw.search(query, limit=1, count=True).count
    seen: dict[tuple[str, int], Hit] = {}
    for _score, addr in raw.search(query, limit=max(total, 1)).hits:
        h = _light_hit(raw.doc(addr), 0.0)
        key = (h.parent_id, h.chunk_seq)
        prior = seen.get(key)
        if prior is None:
            seen[key] = h
        else:
            merged = tuple(dict.fromkeys([*prior.memberships, *h.memberships]))
            seen[key] = dataclasses.replace(prior, memberships=merged)
    return list(seen.values())


def _known_keys(prior: RowTable | None) -> dict[str, str]:
    """Keys a previous run filed each unchanged section under, by stored digest."""
    if prior is None or prior.stored_digest is None:
        return {}
    return dict(zip(prior.stored_digest.tolist(), prior.text_key.tolist(), strict=True))


def _reindex_incomplete() -> bool:
    from fnd.index_runner import saved_states

    return bool(saved_states())


def _rows(
    sections: list[Hit],
    keys: list[str],
    store: VectorStore,
    opted: tuple[str, ...],
    stamp: Any,
    *,
    complete: bool,
) -> RowTable:
    import numpy as np

    from fnd.vectors import RowTable

    snap_rows = {k: i for i, k in enumerate(store.keys())}
    kept = [(h, k) for h, k in zip(sections, keys, strict=True) if k in snap_rows]
    return RowTable(
        parent_id=np.array([h.parent_id for h, _ in kept], dtype=str),
        chunk_seq=np.array([h.chunk_seq for h, _ in kept], dtype=np.int32),
        vec_row=np.array([snap_rows[k] for _, k in kept], dtype=np.int32),
        collections=opted,
        member=np.array(
            [[c in {m for m, _ in h.memberships} for c in opted] for h, _ in kept], dtype=bool
        ).reshape(len(kept), len(opted)),
        text_key=np.array([k for _, k in kept], dtype=str),
        stamp=stamp if isinstance(stamp, tuple) else _NEVER,
        complete=complete,
        stored_digest=np.array([stored_digest(h) for h, _ in kept], dtype=str),
        store_generation=store.generation,
    )


def _batches(files: Iterable[list[tuple[str, str]]], size: int) -> Iterable[list[tuple[str, str]]]:
    """Whole files gathered until a batch holds at least ``size`` sections."""
    batch: list[tuple[str, str]] = []
    for sections in files:
        batch.extend(sections)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def reconcile(
    index_dir: Path,
    *,
    store: VectorStore,
    embedder: Embedder,
    opted_in: frozenset[str],
    batch: int = BATCH,
    flush_every: int = FLUSH_EVERY,
    save_every_s: float = SAVE_EVERY_S,
    clock: Callable[[], float] = time.monotonic,
    should_pause: Callable[[], bool] = lambda: False,
    cancel: Callable[[], bool] = lambda: False,
    on_progress: Callable[[Progress], None] = lambda p: None,
) -> RowTable:
    """Embed every opted-in section the store lacks; the row table as of this index commit."""
    from fnd.query import Searcher, decoded_body
    from fnd.vectors import RowTable

    searcher = Searcher(index_dir=index_dir)
    stamp = searcher.generation
    opted = tuple(sorted(opted_in))
    prior = RowTable.load(store.folder)
    if (
        prior is not None
        and prior.complete
        and prior.stamp == stamp
        and prior.collections == opted
        and prior.store_generation == store.generation
    ):
        used = {str(k) for k in prior.text_key.tolist()}
        unused = len(store) - len(used)
        if all(store.has(k) for k in used) and (unused * 4 <= len(store) or _reindex_incomplete()):
            return prior

    sections = [
        h for h in indexed_sections(searcher) if any(c in opted_in for c, _ in h.memberships)
    ]
    known = _known_keys(prior)
    keys: list[str] = []
    by_file: dict[str, list[tuple[str, str]]] = {}
    queued: set[str] = set()
    digests = [stored_digest(h) for h in sections]
    for h, digest in zip(sections, digests, strict=True):
        key = known.get(digest)
        text = None
        if key is None or not store.has(key):
            text = section_text(h.title, h.heading_path, decoded_body(h)[0])
            key = section_key(embedder.token_ids(text), GRANITE.max_tokens)
        keys.append(key)
        if text is not None and not store.has(key) and key not in queued:
            queued.add(key)
            by_file.setdefault(h.parent_id, []).append((key, text))
    todo = [s for s in by_file.values() if s]
    total = sum(len(s) for s in todo)
    done = 0
    on_progress(Progress(done, total))
    pending_keys: list[str] = []
    pending_vecs: list[Any] = []

    def flush() -> None:
        import numpy as np

        if pending_keys:
            store.append(list(pending_keys), np.vstack(pending_vecs))
            pending_keys.clear()
            pending_vecs.clear()

    last_save = clock()
    try:
        for n, part in enumerate(_batches(todo, batch), 1):
            if cancel():
                break
            while should_pause() and not cancel():
                time.sleep(0.5)
            fresh = [(k, t) for k, t in part if not store.has(k)]
            if fresh:
                pending_keys.extend(k for k, _ in fresh)
                pending_vecs.append(embedder.embed([t for _, t in fresh]))
            done += len(part)
            on_progress(Progress(done, total))
            if n % flush_every == 0:
                flush()
            if clock() - last_save >= save_every_s:
                flush()
                _rows(sections, keys, store, opted, stamp, complete=False).save(store.folder)
                last_save = clock()
    finally:
        # An interrupt keeps every batch already embedded.
        flush()
    if done == total and not _reindex_incomplete():
        store.collect(set(keys))
    if done == total:
        store.merge()
    # After collection: compaction renumbers the rows this table points at.
    rows = _rows(sections, keys, store, opted, stamp, complete=done == total)
    rows.save(store.folder)
    return rows


def _load_embedder(spec: str | None, threads: int) -> Embedder:
    if spec is None:
        from fnd.semantic import load_embedder

        return load_embedder(threads=threads)
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr)()


def _main(argv: list[str]) -> int:
    """``python -m fnd.embed_job INDEX STORE COLLECTIONS [--embedder mod:attr] [--batch N]``."""
    import argparse

    from fnd.cli_output import echo
    from fnd.semantic import GRANITE
    from fnd.vectors import StoreBusyError, VectorStore, store_header

    ap = argparse.ArgumentParser(prog="fnd.embed_job")
    ap.add_argument("index", type=Path)
    ap.add_argument("store", type=Path)
    ap.add_argument("collections")
    ap.add_argument("--embedder")
    ap.add_argument("--batch", type=int, default=BATCH)
    a = ap.parse_args(argv)
    with contextlib.suppress(AttributeError, OSError):
        os.nice(NICE)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("now", True))
    threads = max(1, (os.cpu_count() or 2) // 2)
    embedder = _load_embedder(a.embedder, threads)
    header = store_header(GRANITE)
    if a.embedder is not None:
        header = {**header, "model": a.embedder}
    try:
        store = VectorStore.open_writer(a.store, dim=embedder.dim, header=header)
    except StoreBusyError:
        return BUSY_EXIT
    pause = a.store / "pause"
    try:
        reconcile(
            a.index,
            store=store,
            embedder=embedder,
            opted_in=frozenset(c for c in a.collections.split(",") if c),
            batch=a.batch,
            should_pause=pause.exists,
            cancel=lambda: stop["now"],
            on_progress=lambda p: echo(json.dumps({"done": p.done, "total": p.total})),
        )
    finally:
        store.close()
    return CANCELLED_EXIT if stop["now"] else 0


class EmbedJobRunner:
    """Runs :func:`_main` in a child process and relays its progress."""

    def __init__(self, *, index_dir: Path, store_dir: Path, embedder: str | None = None) -> None:
        self._index_dir = index_dir
        self._store_dir = store_dir
        self._embedder = embedder
        self._proc: asyncio.subprocess.Process | None = None
        self.latest: Progress | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self, opted_in: Iterable[str]) -> None:
        if self.running:
            return
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self.pause(False)
        threads = str(max(1, (os.cpu_count() or 2) // 2))
        argv = [
            sys.executable,
            "-m",
            "fnd.embed_job",
            str(self._index_dir),
            str(self._store_dir),
            ",".join(sorted(opted_in)),
        ]
        if self._embedder is not None:
            argv += ["--embedder", self._embedder]
        env = {**os.environ, "RAYON_NUM_THREADS": threads, "OMP_NUM_THREADS": threads}
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env
        )

    async def progress(self) -> AsyncIterator[Progress]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        async for line in proc.stdout:
            with contextlib.suppress(ValueError, KeyError):
                data = json.loads(line)
                self.latest = Progress(int(data["done"]), int(data["total"]))
                yield self.latest
        await proc.wait()

    def pause(self, flag: bool) -> None:
        marker = self._store_dir / "pause"
        # The folder may be gone (an uninstall from the CLI); a timer raising crashes the app.
        with contextlib.suppress(OSError):
            if flag:
                marker.touch()
            else:
                marker.unlink(missing_ok=True)

    def terminate(self) -> None:
        """Ask the child to stop after its batch, without waiting for it."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            self.pause(False)
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    async def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self.pause(False)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
