"""End to end: a file shared across collections is one document set.

Reproduces the live bug (a cheatsheet in an Obsidian vault reached by two
collections, one rebuilt and one stale, so the preview showed the stale fence
language) and proves the normalised storage resolves it: one copy, freshest
content, membership merged, and correct prune/delete behaviour.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import tantivy

from fnd.extract import ExtractError
from fnd.index import build_index
from fnd.schema import F_COLLECTION, F_MEMBERSHIP, F_PARENT_ID, build_schema, membership_token


def _pid(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def _docs_for(index_dir: Path, parent_id: str) -> list[tantivy.Document]:
    index = tantivy.Index.open(str(index_dir))
    index.reload()
    searcher = index.searcher()
    q = tantivy.Query.term_query(build_schema(), F_PARENT_ID, parent_id)
    return [searcher.doc(addr) for _score, addr in searcher.search(q, limit=1000).hits]


def _collections(docs: list[tantivy.Document]) -> set[str]:
    names: set[str] = set()
    for doc in docs:
        names |= set(doc.get_all(F_COLLECTION))
    return names


def test_two_collections_share_one_document_set(tmp_path: Path, tmp_index_dir: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# H\n\nrisotto and templates\n", encoding="utf-8")

    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C1")
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C2")

    docs = _docs_for(tmp_index_dir, _pid(note))
    assert docs, "the file vanished"
    # Store-once: every chunk belongs to BOTH collections. If it were stored
    # per collection, half the docs would carry only C1 and half only C2.
    for doc in docs:
        assert set(doc.get_all(F_COLLECTION)) == {"C1", "C2"}, (
            "the file was duplicated per collection"
        )
    mem = {t for doc in docs for t in doc.get_all(F_MEMBERSHIP)}
    assert membership_token("C1", "") in mem
    assert membership_token("C2", "") in mem


def test_rebuilding_one_collection_keeps_the_other_and_refreshes_content(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The live bug: one collection stale on `cshtml`, the other rebuilt on
    `csharp`. After the rebuild the file is one copy, fresh, in both."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "cheatsheet.md"

    note.write_text("# Routing\n\n```cshtml\n<div>@x</div>\n```\n", encoding="utf-8")
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="Obsidian")

    note.write_text("# Routing\n\n```csharp\nvar x = 1;\n```\n", encoding="utf-8")
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="WTE")

    docs = _docs_for(tmp_index_dir, _pid(note))
    assert _collections(docs) == {"Obsidian", "WTE"}, "the rebuild dropped the other collection"
    joined = "\n".join(
        bytes(doc.get_first("body_md") or b"").decode("utf-8", "replace") for doc in docs
    )
    assert "```csharp" in joined, joined
    assert "```cshtml" not in joined, "a stale copy of the file survived"


def test_prune_from_one_collection_keeps_the_file_for_another(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    keep = vault / "b.md"
    keep.write_text("# B\n\nbravo\n", encoding="utf-8")

    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C1")
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C2")
    # C1 re-indexes but no longer reaches b.md.
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C1", excludes=["*b.md"])

    docs = _docs_for(tmp_index_dir, _pid(keep))
    assert _collections(docs) == {"C2"}, "b.md should have left C1 but stayed in C2"


def test_a_file_deleted_from_disk_leaves_every_collection(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# H\n\ncontent\n", encoding="utf-8")

    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C1")
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C2")
    parent_id = _pid(note)
    note.unlink()
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C1")

    assert _docs_for(tmp_index_dir, parent_id) == [], "a deleted file lingered in the index"


def test_a_shared_file_survives_when_reduction_cannot_reread_it(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: dropping one collection while a shared file is present but
    unreadable (a cloud placeholder that will not materialise) must NOT delete
    it from the sibling. Deleting on a failed re-read is silent data loss."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# H\n\nshared body\n", encoding="utf-8")
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C1")
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C2")

    import fnd.index as index_mod

    def _unreadable(path: object = "", *_a: object, **_k: object) -> object:
        raise ExtractError(str(path), "evicted")

    monkeypatch.setattr(index_mod, "extract", _unreadable)
    from fnd.index import drop_collection

    drop_collection(tmp_index_dir, "C1")

    docs = _docs_for(tmp_index_dir, _pid(note))
    assert docs, "the shared file was deleted from its sibling on a failed re-read"
    assert "C2" in _collections(docs), "C2 lost a file it still holds"


def test_a_shared_file_survives_an_empty_reduction_read(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other control: an empty re-extract (no chunks, no error) during a
    reduction must also leave the sibling's copy, not delete it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# H\n\nshared body\n", encoding="utf-8")
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C1")
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="C2")

    import fnd.index as index_mod

    def _empty(*_a: object, **_k: object) -> object:
        return iter(())

    monkeypatch.setattr(index_mod, "extract", _empty)
    from fnd.index import drop_collection

    drop_collection(tmp_index_dir, "C1")

    docs = _docs_for(tmp_index_dir, _pid(note))
    assert docs, "the shared file was deleted from its sibling on an empty re-read"
    assert "C2" in _collections(docs), "C2 lost a file it still holds"
