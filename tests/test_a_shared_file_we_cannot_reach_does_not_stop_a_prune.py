"""A shared file under a directory we cannot search keeps its document, and the
prune or drop that reached it still completes."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import tantivy

from fnd.index import build_index, drop_collection
from fnd.schema import F_COLLECTION, F_PARENT_ID, build_schema


@dataclass(frozen=True, slots=True)
class _Corpus:
    vault: Path
    gone: Path
    own: Path
    shared: Path


def _pid(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def _collections_of(index_dir: Path, path: Path) -> set[str]:
    index = tantivy.Index.open(str(index_dir))
    index.reload()
    searcher = index.searcher()
    query = tantivy.Query.term_query(build_schema(), F_PARENT_ID, _pid(path))
    hits = searcher.search(query, limit=1000).hits
    return {str(c) for _s, addr in hits for c in searcher.doc(addr).get_all(F_COLLECTION)}


@pytest.fixture
def locked(tmp_path: Path, tmp_index_dir: Path) -> Iterator[_Corpus]:
    """C1 holds a vault and a share; C2 holds the share; the share's volume is then mode 000."""
    vault = tmp_path / "vault"
    vault.mkdir()
    own = vault / "own.md"
    own.write_text("# Own\n\nown body\n", encoding="utf-8")
    gone = vault / "gone.md"
    gone.write_text("# Gone\n\ngone body\n", encoding="utf-8")
    volume = tmp_path / "Volume"
    inner = volume / "Notes"
    inner.mkdir(parents=True)
    shared = inner / "shared.md"
    shared.write_text("# Shared\n\nshared body\n", encoding="utf-8")
    build_index(roots=[vault, inner], index_dir=tmp_index_dir, collection="C1")
    build_index(roots=[inner], index_dir=tmp_index_dir, collection="C2")

    os.chmod(volume, 0o000)
    try:
        if os.access(volume, os.R_OK):
            pytest.skip("running as a user that bypasses directory permissions")
        yield _Corpus(vault=vault, gone=gone, own=own, shared=shared)
    finally:
        os.chmod(volume, stat.S_IRWXU)


def test_an_update_that_no_longer_reaches_a_locked_shared_file_still_prunes(
    locked: _Corpus, tmp_index_dir: Path
) -> None:
    """The prune finishes: a deleted file leaves, the locked shared file stays in C2."""
    locked.gone.unlink()

    build_index(roots=[locked.vault], index_dir=tmp_index_dir, collection="C1")

    assert _collections_of(tmp_index_dir, locked.gone) == set()
    assert "C2" in _collections_of(tmp_index_dir, locked.shared)


def test_dropping_a_collection_keeps_a_locked_shared_file_for_its_sibling(
    locked: _Corpus, tmp_index_dir: Path
) -> None:
    """The drop finishes: C1's own file leaves, the locked shared file stays in C2."""
    drop_collection(tmp_index_dir, "C1")

    assert _collections_of(tmp_index_dir, locked.own) == set()
    assert "C2" in _collections_of(tmp_index_dir, locked.shared)
