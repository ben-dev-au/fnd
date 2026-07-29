"""Re-indexing must drop chunks for files that have left the collection.

A file can leave four ways: deleted from disk, newly excluded by a glob, no
longer matching the source's ``frontmatter_filter``, or having its whole
source dropped from the config. In every case an incremental re-index (no
``--rebuild``) has to prune the stale chunks, or the index keeps serving hits
for content the collection no longer contains.

Pruning must stay narrow: it is scoped to one collection so a shared vault
keeps its sibling's chunks, it leaves the content-addressed texture cache
alone (``fnd cache prune-orphans`` owns that, across all collections), and it
is skipped whenever the walk was incomplete — a cancelled run or an offline
source root would otherwise read as "every file was deleted".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.index import build_index_from_config
from fnd.index_runner import run_indexer
from fnd.query import Searcher


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _names(index_dir: Path, query: str, collection: str) -> set[str]:
    hits = Searcher(index_dir=index_dir).search(query, limit=50, collection=collection)
    return {Path(h.path).name for h in hits}


def test_reindex_drops_file_deleted_from_disk(tmp_path: Path, tmp_index_dir: Path) -> None:
    notes = tmp_path / "notes"
    _touch(notes / "keep.md", "# Keep\npenguin sandwich\n")
    _touch(notes / "gone.md", "# Gone\npenguin sandwich\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])

    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    assert _names(tmp_index_dir, "penguin", "notes") == {"keep.md", "gone.md"}

    (notes / "gone.md").unlink()
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)

    assert _names(tmp_index_dir, "penguin", "notes") == {"keep.md"}


def test_reindex_drops_file_newly_excluded(tmp_path: Path, tmp_index_dir: Path) -> None:
    notes = tmp_path / "notes"
    _touch(notes / "keep.md", "# Keep\npenguin sandwich\n")
    _touch(notes / "drafts" / "wip.md", "# WIP\npenguin sandwich\n")
    before = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])

    build_index_from_config(config=before, collection="notes", index_dir=tmp_index_dir)
    assert "wip.md" in _names(tmp_index_dir, "penguin", "notes")

    after = CollectionConfig(
        sources=[SourceConfig(path=notes, includes=["**/*.md"], excludes=["drafts/**"])]
    )
    build_index_from_config(config=after, collection="notes", index_dir=tmp_index_dir)

    assert _names(tmp_index_dir, "penguin", "notes") == {"keep.md"}


def test_reindex_drops_file_failing_frontmatter_filter(tmp_path: Path, tmp_index_dir: Path) -> None:
    notes = tmp_path / "notes"
    _touch(notes / "public.md", "---\ntags:\n  - open\n---\n# Public\npenguin sandwich\n")
    _touch(notes / "secret.md", "---\ntags:\n  - private\n---\n# Secret\npenguin sandwich\n")
    before = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])

    build_index_from_config(config=before, collection="notes", index_dir=tmp_index_dir)
    assert "secret.md" in _names(tmp_index_dir, "penguin", "notes")

    after = CollectionConfig(
        sources=[
            SourceConfig(
                path=notes,
                includes=["**/*.md"],
                frontmatter_filter="NOT ('private' in tags)",
            )
        ]
    )
    build_index_from_config(config=after, collection="notes", index_dir=tmp_index_dir)

    assert _names(tmp_index_dir, "penguin", "notes") == {"public.md"}


def test_reindex_keeps_chunks_when_source_root_is_missing(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """An offline volume must not read as "every file was deleted".

    ``walk`` yields nothing for a missing root instead of raising, so an
    unguarded prune would erase the collection whenever an external drive or
    an iCloud folder was temporarily unavailable.
    """
    notes = tmp_path / "removable"
    _touch(notes / "a.md", "# A\npenguin sandwich\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])

    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    assert _names(tmp_index_dir, "penguin", "notes") == {"a.md"}

    # Simulate the volume disappearing, not the file being deleted.
    (notes / "a.md").unlink()
    notes.rmdir()
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)

    assert _names(tmp_index_dir, "penguin", "notes") == {"a.md"}


def test_reindex_drops_source_removed_from_config(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Dropping a whole source from the collection prunes its files."""
    a, b = tmp_path / "a", tmp_path / "b"
    _touch(a / "keep.md", "# Keep\npenguin sandwich\n")
    _touch(b / "drop.md", "# Drop\npenguin sandwich\n")
    before = CollectionConfig(
        sources=[
            SourceConfig(path=a, includes=["**/*.md"]),
            SourceConfig(path=b, includes=["**/*.md"]),
        ]
    )
    build_index_from_config(config=before, collection="notes", index_dir=tmp_index_dir)
    assert _names(tmp_index_dir, "penguin", "notes") == {"keep.md", "drop.md"}

    after = CollectionConfig(sources=[SourceConfig(path=a, includes=["**/*.md"])])
    build_index_from_config(config=after, collection="notes", index_dir=tmp_index_dir)

    assert _names(tmp_index_dir, "penguin", "notes") == {"keep.md"}


def test_reindex_keeps_sibling_collection_chunks(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Pruning must be collection-scoped: a shared vault indexed under two
    collections keeps the sibling's chunks when one is re-indexed."""
    notes = tmp_path / "vault"
    _touch(notes / "shared.md", "# Shared\npenguin sandwich\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])

    build_index_from_config(config=cc, collection="alpha", index_dir=tmp_index_dir)
    build_index_from_config(config=cc, collection="beta", index_dir=tmp_index_dir)

    build_index_from_config(config=cc, collection="alpha", index_dir=tmp_index_dir)

    assert _names(tmp_index_dir, "penguin", "alpha") == {"shared.md"}
    assert _names(tmp_index_dir, "penguin", "beta") == {"shared.md"}


def test_prune_from_one_collection_leaves_shared_file_in_the_other(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Removing a shared file from one collection must not evict it from
    the other. The same vault under two collections is the common case."""
    notes = tmp_path / "vault"
    _touch(notes / "shared.md", "# Shared\npenguin sandwich\n")
    both = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])

    build_index_from_config(config=both, collection="alpha", index_dir=tmp_index_dir)
    build_index_from_config(config=both, collection="beta", index_dir=tmp_index_dir)
    assert _names(tmp_index_dir, "penguin", "alpha") == {"shared.md"}

    # Drop it from alpha only, by narrowing alpha's globs.
    narrowed = CollectionConfig(
        sources=[SourceConfig(path=notes, includes=["**/*.md"], excludes=["shared.md"])]
    )
    build_index_from_config(config=narrowed, collection="alpha", index_dir=tmp_index_dir)

    assert _names(tmp_index_dir, "penguin", "alpha") == set()
    assert _names(tmp_index_dir, "penguin", "beta") == {"shared.md"}


def test_pruned_pdf_keeps_texture_cache_entry_when_another_collection_holds_it(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The texture cache is content-addressed and shared across collections,
    so a per-collection prune must never orphan an entry another collection
    still needs. ``live_content_shas`` spans every collection, which is what
    ``fnd cache prune-orphans`` acts on."""
    from fnd.cache import sha256_file
    from fnd.config import Config
    from fnd.texture_maintenance import live_content_shas

    src = tmp_path / "papers"
    src.mkdir()
    pdf = src / "paper.pdf"
    pdf.write_bytes((Path(__file__).parent / "fixtures" / "papers" / "test.pdf").read_bytes())
    sha = sha256_file(pdf)

    alpha_narrowed = CollectionConfig(
        sources=[SourceConfig(path=src, includes=["**/*.pdf"], excludes=["paper.pdf"])]
    )
    beta = CollectionConfig(sources=[SourceConfig(path=src, includes=["**/*.pdf"])])
    cfg = Config(collections={"alpha": alpha_narrowed, "beta": beta})

    # Dropped from alpha, still reachable via beta — so still live content.
    assert sha in live_content_shas(cfg)

    # Gone from every collection — now genuinely an orphan.
    cfg_none = Config(collections={"alpha": alpha_narrowed})
    assert sha not in live_content_shas(cfg_none)


async def _run(cfg: CollectionConfig, collection: str, index_dir: Path, **kw: object) -> None:
    async for _ev in run_indexer(config=cfg, collection=collection, index_dir=index_dir, **kw):  # type: ignore[arg-type]
        pass


@pytest.mark.asyncio
async def test_runner_prunes_file_deleted_from_disk(tmp_path: Path) -> None:
    """The TUI indexer path prunes too — it is what most re-indexes go through."""
    notes = tmp_path / "notes"
    _touch(notes / "keep.md", "# Keep\npenguin sandwich\n")
    _touch(notes / "gone.md", "# Gone\npenguin sandwich\n")
    idx = tmp_path / "idx"
    cfg = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])

    await _run(cfg, "notes", idx, state_path=tmp_path / "s.toml")
    assert _names(idx, "penguin", "notes") == {"keep.md", "gone.md"}

    (notes / "gone.md").unlink()
    await _run(cfg, "notes", idx, state_path=tmp_path / "s.toml")

    assert _names(idx, "penguin", "notes") == {"keep.md"}


@pytest.mark.asyncio
async def test_runner_does_not_prune_when_cancelled(tmp_path: Path) -> None:
    """A cancelled run walked only part of the corpus, so its live set is
    incomplete — pruning against it would delete files that still exist."""
    notes = tmp_path / "notes"
    for i in range(6):
        _touch(notes / f"n{i}.md", f"# N{i}\npenguin sandwich {i}\n")
    idx = tmp_path / "idx"
    cfg = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])

    await _run(cfg, "notes", idx, state_path=tmp_path / "s.toml")
    assert len(_names(idx, "penguin", "notes")) == 6

    # Cancel after the first file_complete.
    cancel = asyncio.Event()
    async for ev in run_indexer(
        config=cfg,
        collection="notes",
        index_dir=idx,
        state_path=tmp_path / "s2.toml",
        cancel=cancel,
    ):
        if ev.kind == "file_complete":
            cancel.set()

    assert len(_names(idx, "penguin", "notes")) == 6
