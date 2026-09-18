"""A file indexed under two collections is stored twice, and the preview served
the STALE copy.

An Obsidian vault reached through two collections is indexed once per
collection. Rebuild one and not the other and the copies DIVERGE: the stale
one still holds the old fenced-code languages, so the preview rendered
`cshtml` (an unknown lexer, no colour) from a collection the user never
rebuilt, while their freshly rebuilt collection held `csharp`. The dedup kept
whichever tantivy returned first, which was the older copy.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from fnd.index import build_index
from fnd.query import FileChunk, Searcher, _freshest_per_chunk_seq


def _chunk(seq: int, mtime: int, body_md: str) -> FileChunk:
    return FileChunk(
        parent_id="p",
        path="/vault/cheatsheet.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="",
        chunk_seq=seq,
        blocks=[],
        body_md=body_md,
        mtime=mtime,
    )


def test_the_freshest_copy_wins_however_the_copies_are_ordered() -> None:
    """The deterministic guard: the STALE copy is listed first (as tantivy
    returns the older, lower-address docs), and the newer one must still win."""
    stale = _chunk(0, mtime=1000, body_md="```cshtml\n<div>@x</div>\n```")
    fresh = _chunk(0, mtime=2000, body_md="```csharp\nvar x = 1;\n```")

    kept = _freshest_per_chunk_seq([stale, fresh])

    assert len(kept) == 1
    assert kept[0].body_md == fresh.body_md, "the stale copy shadowed the rebuilt one"


def test_identical_copies_collapse_to_one_in_order() -> None:
    """The control: dedup still collapses N copies to one per chunk_seq, in
    document order, so a shared file is not previewed N times over."""
    chunks = [
        _chunk(1, mtime=5, body_md="b"),
        _chunk(0, mtime=5, body_md="a"),
        _chunk(0, mtime=5, body_md="a"),
        _chunk(2, mtime=5, body_md="c"),
    ]

    kept = _freshest_per_chunk_seq(chunks)

    assert [c.chunk_seq for c in kept] == [0, 1, 2]


def test_get_file_chunks_carries_mtime_end_to_end(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The plumbing: two real collections index the same file at two mtimes, and
    the fresh content reaches the preview with the stale one gone."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "cheatsheet.md"

    note.write_text("# Routing\n\n```cshtml\n<div>@Model.Name</div>\n```\n", encoding="utf-8")
    old = time.time() - 4 * 86400
    os.utime(note, (old, old))
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="stale")

    note.write_text("# Routing\n\n```csharp\nvar name = Model.Name;\n```\n", encoding="utf-8")
    new = time.time()
    os.utime(note, (new, new))
    build_index(roots=[vault], index_dir=tmp_index_dir, collection="fresh")

    parent_id = hashlib.sha1(str(note.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()
    chunks = Searcher(index_dir=tmp_index_dir).get_file_chunks(parent_id)
    joined = "\n".join(c.body_md for c in chunks)

    assert "```csharp" in joined, joined
    assert "```cshtml" not in joined, "the stale copy shadowed the rebuilt one"
    assert all(c.mtime > 0 for c in chunks), "mtime never reached the FileChunk"
