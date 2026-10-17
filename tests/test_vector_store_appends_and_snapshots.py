"""Vectors are appended, never rewritten, and readers see consistent snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from fnd.vectors import RowTable, VectorStore  # noqa: E402

H = {"model": "tiny", "revision": "abc", "max_tokens": 8, "recipe": "r"}


def _append(folder: Path, header: Mapping[str, object], keys: list[str], vecs: object) -> None:
    with VectorStore.open_writer(folder, dim=4, header=dict(header)) as w:
        w.append(keys, vecs)  # type: ignore[arg-type]


def _v(n: int, seed: int = 0):
    r = np.random.default_rng(seed).normal(size=(n, 4)).astype(np.float32)
    return r / np.linalg.norm(r, axis=1, keepdims=True)


def test_append_adds_a_segment_and_keeps_the_old(tmp_path: Path) -> None:
    """A second append writes a new segment; the first is untouched."""
    s = VectorStore.open_writer(tmp_path, dim=4, header=H)
    s.append(["a", "b"], _v(2))
    first = sorted(tmp_path.glob("seg-*.npy"))[0]
    stamp = first.stat().st_mtime_ns
    s.append(["c"], _v(1, seed=1))
    assert first.stat().st_mtime_ns == stamp
    assert s.has("c")
    assert s.snapshot().vecs.shape == (3, 4)


def test_a_reopened_store_holds_what_was_saved(tmp_path: Path) -> None:
    """Closing and reopening loses nothing that was appended."""
    vecs = _v(2)
    _append(tmp_path, H, ["a", "b"], vecs)
    snap = VectorStore.open(tmp_path, dim=4, header=H).snapshot()
    assert np.allclose(snap.vecs[snap.key_row["b"]], vecs[1], atol=1e-3)


def test_a_header_mismatch_starts_empty(tmp_path: Path) -> None:
    """A different model revision or recipe re-embeds from nothing."""
    _append(tmp_path, H, ["a"], _v(1))
    s = VectorStore.open_writer(tmp_path, dim=4, header={**H, "revision": "new"})
    assert not s.has("a")


def test_an_old_snapshot_survives_an_append(tmp_path: Path) -> None:
    """A reader's snapshot keeps its rows while the writer appends."""
    s = VectorStore.open_writer(tmp_path, dim=4, header=H)
    s.append(["a"], _v(1))
    snap = s.snapshot()
    s.append(["b"], _v(1, seed=2))
    assert snap.vecs.shape[0] == 1
    assert "b" not in snap.key_row


def test_collect_compacts_only_past_a_quarter_unused(tmp_path: Path) -> None:
    """Collection rewrites the store only when over a quarter of rows are unused."""
    s = VectorStore.open_writer(tmp_path, dim=4, header=H)
    vecs = _v(8)
    s.append(list("abcdefgh"), vecs)
    assert s.collect(set("abcdefg")) == 0
    assert s.collect(set("abcde")) == 3
    s.close()
    snap = VectorStore.open(tmp_path, dim=4, header=H).snapshot()
    assert snap.vecs.shape[0] == 5
    assert "h" not in snap.key_row
    assert np.allclose(snap.vecs[snap.key_row["e"]], vecs[4], atol=1e-3)


def test_appending_after_a_collection_keeps_the_survivors(tmp_path: Path) -> None:
    """A segment written after compaction never overwrites the compacted one."""
    s = VectorStore.open_writer(tmp_path, dim=4, header=H)
    s.append(list("abcd"), _v(4))
    s.collect({"a"})
    s.append(["z"], _v(1, seed=3))
    s.close()
    reopened = VectorStore.open(tmp_path, dim=4, header=H)
    assert reopened.has("a")
    assert reopened.has("z")


def test_the_row_table_round_trips(tmp_path: Path) -> None:
    """A saved row table loads back field for field."""
    rows = RowTable(
        parent_id=np.array(["p1", "p2"]),
        chunk_seq=np.array([0, 3], dtype=np.int32),
        vec_row=np.array([1, 0], dtype=np.int32),
        collections=("notes", "books"),
        member=np.array([[True, False], [True, True]]),
        text_key=np.array(["k1", "k2"]),
        stamp=("idx", 5, 6),
    )
    rows.save(tmp_path)
    back = RowTable.load(tmp_path)
    assert back is not None
    assert back.collections == rows.collections
    assert back.stamp == rows.stamp
    assert back.parent_id.tolist() == ["p1", "p2"]
    assert back.member.tolist() == [[True, False], [True, True]]


def test_no_row_table_loads_as_none(tmp_path: Path) -> None:
    """A store that never finished a reconcile has no row table."""
    assert RowTable.load(tmp_path) is None


def test_segments_no_manifest_names_are_swept_on_open(tmp_path: Path) -> None:
    """A segment compaction could not delete (held open on Windows) goes at the next open."""
    s = VectorStore.open_writer(tmp_path, dim=4, header=H)
    s.append(["a"], _v(1))
    (tmp_path / "seg-99.npy").write_bytes(b"stale")
    (tmp_path / "seg-99.keys.json").write_text("[]")
    s.close()
    VectorStore.open_writer(tmp_path, dim=4, header=H).close()
    assert not (tmp_path / "seg-99.npy").exists()
    assert not (tmp_path / "seg-99.keys.json").exists()


def test_a_second_writer_is_refused(tmp_path: Path) -> None:
    """Only one process may write the store; a second is told it is busy."""
    from fnd.vectors import StoreBusyError

    first = VectorStore.open_writer(tmp_path, dim=4, header=H)
    with pytest.raises(StoreBusyError):
        VectorStore.open_writer(tmp_path, dim=4, header=H)
    first.close()
    VectorStore.open_writer(tmp_path, dim=4, header=H).close()


def test_a_killed_writers_lock_is_free_at_once(tmp_path: Path) -> None:
    """The lock dies with its process, however it ended; no ten-minute wait."""
    import subprocess
    import sys

    code = (
        "import os, sys, pathlib; from fnd.vectors import VectorStore; "
        f"VectorStore.open_writer(pathlib.Path(sys.argv[1]), dim=4, header={H!r}); os._exit(9)"
    )
    done = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False)
    assert done.returncode == 9
    assert (tmp_path / "writer.lock").exists()
    VectorStore.open_writer(tmp_path, dim=4, header=H).close()


def test_a_live_writer_keeps_its_lock_however_long_it_idles(tmp_path: Path) -> None:
    """An old lock file does not mean a dead writer: a paused job still owns the store."""
    import os

    from fnd.vectors import StoreBusyError

    first = VectorStore.open_writer(tmp_path, dim=4, header=H)
    lock = tmp_path / "writer.lock"
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    with pytest.raises(StoreBusyError):
        VectorStore.open_writer(tmp_path, dim=4, header=H)
    first.close()


def test_a_reader_retries_a_snapshot_a_merge_swept(tmp_path: Path) -> None:
    """A reader opened before a merge still gets the merged vectors, not FileNotFoundError."""
    w = VectorStore.open_writer(tmp_path, dim=4, header=H)
    w.append(["a"], _v(1))
    w.append(["b"], _v(1, 1))
    reader = VectorStore.open(tmp_path, dim=4, header=H)
    w.merge()
    w.close()
    snap = reader.snapshot()
    assert set(snap.key_row) == {"a", "b"}
    assert snap.vecs.shape == (2, 4)


def test_a_reader_never_deletes(tmp_path: Path) -> None:
    """Opening to read leaves unnamed segments and a mismatched store alone."""
    w = VectorStore.open_writer(tmp_path, dim=4, header=H)
    w.append(["a"], _v(1))
    w.close()
    (tmp_path / "seg-99.npy").write_bytes(b"in flight")
    VectorStore.open(tmp_path, dim=4, header=H)
    VectorStore.open(tmp_path, dim=4, header={**H, "revision": "other"})
    assert (tmp_path / "seg-99.npy").exists()
    assert VectorStore.open(tmp_path, dim=4, header=H).has("a")


def test_rows_filed_before_a_compaction_are_not_used(tmp_path: Path) -> None:
    """A row table points at row numbers; after compaction renumbers them it is stale."""
    w = VectorStore.open_writer(tmp_path, dim=4, header=H)
    w.append(list("abcd"), _v(4))
    before = w.generation
    w.collect({"a"})
    assert w.generation != before
    w.close()


def test_a_merged_store_is_one_memory_mapped_segment(tmp_path: Path) -> None:
    """After a run the segments merge, and readers map the vectors rather than copy them."""
    w = VectorStore.open_writer(tmp_path, dim=4, header=H)
    for i in range(5):
        w.append([f"k{i}"], _v(1, seed=i))
    w.merge()
    w.close()
    assert len(list(tmp_path.glob("seg-*.npy"))) == 1
    snap = VectorStore.open(tmp_path, dim=4, header=H).snapshot()
    assert isinstance(snap.vecs, np.memmap)
    assert snap.key_row["k3"] == 3


def test_a_compaction_between_open_and_snapshot_reads_as_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows numbered for the old store must never be paired with compacted vectors."""
    from types import SimpleNamespace

    from fnd import semantic_search
    from fnd.semantic import GRANITE
    from fnd.vectors import store_dir, store_header

    folder = store_dir(GRANITE)
    header = store_header(GRANITE)
    w = VectorStore.open_writer(folder, dim=4, header=header)
    w.append(["a", "b", "c", "d"], _v(4))
    w.append(["e"], _v(1, 1))
    RowTable(
        parent_id=np.array(["p"]),
        chunk_seq=np.array([0], dtype=np.int32),
        vec_row=np.array([2], dtype=np.int32),
        collections=("notes",),
        member=np.array([[True]]),
        text_key=np.array(["c"]),
        stamp=("idx", 1, 1),
    ).save(folder)
    real_open = VectorStore.open.__func__  # type: ignore[attr-defined]
    compacted: list[bool] = []

    def open_then_compact(cls: type[VectorStore], *a: object, **k: object) -> VectorStore:
        reader = real_open(cls, *a, **k)
        if not compacted:
            compacted.append(w.collect({"e"}) > 0)
        return reader

    monkeypatch.setattr(VectorStore, "open", classmethod(open_then_compact))
    assert semantic_search.load_context(lambda: SimpleNamespace(dim=4)) is None  # type: ignore[arg-type]
    assert compacted == [True]
    w.close()
