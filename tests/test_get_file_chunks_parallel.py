"""Phase 4: parallel chunk decode in ``Searcher.get_file_chunks``.

The contract these tests pin:

* Serial and parallel decode produce **identical** chunk lists. Decode
  order isn't a contract — the result is sorted by ``chunk_seq`` either
  way — so test on the sorted list directly.
* Below the parallel-decode threshold the worker pool is not spun up,
  even when ``max_workers > 1``. This avoids the thread-startup tax for
  small markdown files / short PDFs that don't benefit.
* Above the threshold and with ``max_workers > 1``, decode is dispatched
  through the thread pool. We assert via a spy that ``_decode_chunk``
  was invoked from threads other than the calling thread.

The fixture corpus is too small to exceed the 50-chunk threshold on its
own, so the "above threshold" test monkeypatches the threshold low.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from fnd import query as query_mod
from fnd.index import build_index
from fnd.query import Searcher


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _find_parent_id(searcher: Searcher, path_suffix: str) -> str:
    """Pick the parent_id of a file in the fixture corpus by path suffix."""
    hits = searcher.search("blue penguin sandwich", limit=5)
    for h in hits:
        if h.path.endswith(path_suffix):
            return h.parent_id
    raise AssertionError(f"no hit for suffix {path_suffix!r}")


def test_serial_and_parallel_decode_produce_identical_chunks(built_index: Path) -> None:
    """Same inputs, same chunks — regardless of decode path."""
    searcher = Searcher(index_dir=built_index)
    parent_id = _find_parent_id(searcher, "papers/test.pdf")

    serial = searcher.get_file_chunks(parent_id)
    parallel = searcher.get_file_chunks(parent_id, max_workers=4)

    assert serial == parallel, "parallel decode produced a different chunk list than serial"
    assert serial, "expected the fixture PDF to have at least one chunk"


def test_default_max_workers_is_serial(built_index: Path) -> None:
    """``max_workers=None`` (the default) keeps the historic serial
    decode path so existing callers don't accidentally pay thread tax."""
    searcher = Searcher(index_dir=built_index)
    parent_id = _find_parent_id(searcher, "papers/test.pdf")

    saw_threads: set[int] = set()
    real_decode = searcher._decode_chunk

    def spy(searcher_arg: object, address: object) -> object:
        saw_threads.add(threading.get_ident())
        return real_decode(searcher_arg, address)

    searcher._decode_chunk = spy  # type: ignore[method-assign]
    searcher.get_file_chunks(parent_id)
    assert saw_threads == {threading.get_ident()}, (
        f"default path used auxiliary threads: {saw_threads}"
    )


def test_below_threshold_stays_serial_even_with_workers(
    built_index: Path,
) -> None:
    """Small chunk counts skip the thread pool to avoid startup overhead."""
    searcher = Searcher(index_dir=built_index)
    parent_id = _find_parent_id(searcher, "papers/test.pdf")
    n_chunks = len(searcher.get_file_chunks(parent_id))
    # Sanity: the fixture corpus must be smaller than the threshold for
    # this test to be meaningful.
    assert n_chunks < query_mod._PARALLEL_DECODE_THRESHOLD, (
        f"fixture corpus has {n_chunks} chunks; threshold is "
        f"{query_mod._PARALLEL_DECODE_THRESHOLD}. Bump the threshold or "
        f"shrink the fixture so this test stays meaningful."
    )

    saw_threads: set[int] = set()
    real_decode = searcher._decode_chunk

    def spy(searcher_arg: object, address: object) -> object:
        saw_threads.add(threading.get_ident())
        return real_decode(searcher_arg, address)

    searcher._decode_chunk = spy  # type: ignore[method-assign]
    searcher.get_file_chunks(parent_id, max_workers=4)
    assert saw_threads == {threading.get_ident()}, (
        f"below threshold should stay serial; saw threads {saw_threads}"
    )


def test_above_threshold_dispatches_to_thread_pool(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When chunk count crosses the threshold AND ``max_workers > 1``,
    decode runs in worker threads. Monkeypatch the threshold to 1 so the
    small fixture corpus triggers the parallel path."""
    monkeypatch.setattr(query_mod, "_PARALLEL_DECODE_THRESHOLD", 1)
    searcher = Searcher(index_dir=built_index)
    parent_id = _find_parent_id(searcher, "papers/test.pdf")

    saw_threads: set[int] = set()
    real_decode = searcher._decode_chunk

    def spy(searcher_arg: object, address: object) -> object:
        saw_threads.add(threading.get_ident())
        return real_decode(searcher_arg, address)

    searcher._decode_chunk = spy  # type: ignore[method-assign]
    main_thread = threading.get_ident()
    searcher.get_file_chunks(parent_id, max_workers=4)
    worker_threads = saw_threads - {main_thread}
    assert worker_threads, f"parallel path should use auxiliary threads; saw only {saw_threads}"


def test_max_workers_one_stays_serial(built_index: Path) -> None:
    """``max_workers=1`` is an explicit opt-out — no thread pool even
    when over the threshold."""
    searcher = Searcher(index_dir=built_index)
    parent_id = _find_parent_id(searcher, "papers/test.pdf")

    saw_threads: set[int] = set()
    real_decode = searcher._decode_chunk

    def spy(searcher_arg: object, address: object) -> object:
        saw_threads.add(threading.get_ident())
        return real_decode(searcher_arg, address)

    searcher._decode_chunk = spy  # type: ignore[method-assign]
    searcher.get_file_chunks(parent_id, max_workers=1)
    assert saw_threads == {threading.get_ident()}, (
        f"max_workers=1 must stay serial; saw {saw_threads}"
    )
