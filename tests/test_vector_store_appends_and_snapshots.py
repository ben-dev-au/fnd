"""Vectors are appended, never rewritten, and readers see consistent snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from fnd.vectors import RowTable, VectorStore  # noqa: E402

H = {"model": "tiny", "revision": "abc", "max_tokens": 8, "recipe": "r"}


def _v(n: int, seed: int = 0):
    r = np.random.default_rng(seed).normal(size=(n, 4)).astype(np.float32)
    return r / np.linalg.norm(r, axis=1, keepdims=True)


def test_append_adds_a_segment_and_keeps_the_old(tmp_path: Path) -> None:
    """A second append writes a new segment; the first is untouched."""
    s = VectorStore.open(tmp_path, dim=4, header=H)
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
    VectorStore.open(tmp_path, dim=4, header=H).append(["a", "b"], vecs)
    snap = VectorStore.open(tmp_path, dim=4, header=H).snapshot()
    assert np.allclose(snap.vecs[snap.key_row["b"]], vecs[1], atol=1e-3)


def test_a_header_mismatch_starts_empty(tmp_path: Path) -> None:
    """A different model revision or recipe re-embeds from nothing."""
    VectorStore.open(tmp_path, dim=4, header=H).append(["a"], _v(1))
    s = VectorStore.open(tmp_path, dim=4, header={**H, "revision": "new"})
    assert not s.has("a")


def test_an_old_snapshot_survives_an_append(tmp_path: Path) -> None:
    """A reader's snapshot keeps its rows while the writer appends."""
    s = VectorStore.open(tmp_path, dim=4, header=H)
    s.append(["a"], _v(1))
    snap = s.snapshot()
    s.append(["b"], _v(1, seed=2))
    assert snap.vecs.shape[0] == 1
    assert "b" not in snap.key_row


def test_collect_compacts_only_past_a_quarter_unused(tmp_path: Path) -> None:
    """Collection rewrites the store only when over a quarter of rows are unused."""
    s = VectorStore.open(tmp_path, dim=4, header=H)
    vecs = _v(8)
    s.append(list("abcdefgh"), vecs)
    assert s.collect(set("abcdefg")) == 0
    assert s.collect(set("abcde")) == 3
    snap = VectorStore.open(tmp_path, dim=4, header=H).snapshot()
    assert snap.vecs.shape[0] == 5
    assert "h" not in snap.key_row
    assert np.allclose(snap.vecs[snap.key_row["e"]], vecs[4], atol=1e-3)


def test_appending_after_a_collection_keeps_the_survivors(tmp_path: Path) -> None:
    """A segment written after compaction never overwrites the compacted one."""
    s = VectorStore.open(tmp_path, dim=4, header=H)
    s.append(list("abcd"), _v(4))
    s.collect({"a"})
    s.append(["z"], _v(1, seed=3))
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
    s = VectorStore.open(tmp_path, dim=4, header=H)
    s.append(["a"], _v(1))
    (tmp_path / "seg-99.npy").write_bytes(b"stale")
    (tmp_path / "seg-99.keys.json").write_text("[]")
    VectorStore.open(tmp_path, dim=4, header=H)
    assert not (tmp_path / "seg-99.npy").exists()
    assert not (tmp_path / "seg-99.keys.json").exists()
