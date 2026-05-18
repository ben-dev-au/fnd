"""Phase 3 acceptance: collections + includes/excludes precedence."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import CollectionConfig
from fnd.index import build_index, build_index_from_config
from fnd.query import Searcher


def _make_md(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@pytest.fixture
def shaped_corpus(tmp_path: Path) -> Path:
    """Build a synthetic corpus with subdirectories so we can test glob rules.

    Layout::

        root/
          keep/
            wanted.md       — body: "phrase keepable"
          drafts/
            skip-me.md      — body: "phrase keepable" (must be excluded)
          archive/
            old.md          — body: "phrase keepable" (must be excluded)
          .hidden/
            hidden.md       — body: "phrase keepable" (must be excluded)
    """
    root = tmp_path / "root"
    _make_md(root / "keep" / "wanted.md", "# Wanted\nphrase keepable here.\n")
    _make_md(root / "drafts" / "skip-me.md", "# Draft\nphrase keepable in draft.\n")
    _make_md(root / "archive" / "old.md", "# Old\nphrase keepable in archive.\n")
    _make_md(root / ".hidden" / "hidden.md", "# Hidden\nphrase keepable in hidden.\n")
    return root


def test_excludes_drop_subdir_of_included_root(shaped_corpus: Path, tmp_index_dir: Path) -> None:
    """The headline §8 case: include a root, exclude one of its subdirs."""
    written = build_index(
        roots=[shaped_corpus],
        index_dir=tmp_index_dir,
        collection="papers",
        includes=["**/*.md"],
        excludes=["drafts/**", "archive/**"],
    )
    assert written > 0
    hits = Searcher(index_dir=tmp_index_dir).search("keepable", limit=10)
    paths = [h.path for h in hits]
    assert any(p.endswith("keep/wanted.md") for p in paths), f"missing wanted.md in {paths}"
    assert not any("drafts/" in p for p in paths), f"drafts leaked: {paths}"
    assert not any("archive/" in p for p in paths), f"archive leaked: {paths}"


def test_hidden_dirs_excluded_by_default(shaped_corpus: Path, tmp_index_dir: Path) -> None:
    build_index(
        roots=[shaped_corpus],
        index_dir=tmp_index_dir,
        collection="papers",
        includes=["**/*.md"],
    )
    hits = Searcher(index_dir=tmp_index_dir).search("keepable", limit=10)
    paths = [h.path for h in hits]
    assert not any(".hidden" in p for p in paths), f".hidden leaked: {paths}"


def test_collection_field_scopes_query(
    shaped_corpus: Path, tmp_index_dir: Path, tmp_path: Path
) -> None:
    """Two collections in the same index; searching one must not return the other."""
    other = tmp_path / "other"
    _make_md(other / "note.md", "# Note\nkeepable in the OTHER collection.\n")

    build_index(
        roots=[shaped_corpus / "keep"],
        index_dir=tmp_index_dir,
        collection="papers",
    )
    build_index(
        roots=[other],
        index_dir=tmp_index_dir,
        collection="notes",
    )
    s = Searcher(index_dir=tmp_index_dir)
    papers_hits = s.search("keepable", collection="papers", limit=10)
    notes_hits = s.search("keepable", collection="notes", limit=10)
    assert all("keep/wanted.md" in h.path for h in papers_hits), [h.path for h in papers_hits]
    assert all("other/note.md" in h.path for h in notes_hits), [h.path for h in notes_hits]


def test_per_collection_rebuild_does_not_disturb_others(
    shaped_corpus: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Rebuilding `papers` must NOT remove any chunks from `notes`."""
    other = tmp_path / "other"
    _make_md(other / "stable.md", "# Stable\nstable phrase here.\n")

    build_index(
        roots=[shaped_corpus / "keep"],
        index_dir=tmp_index_dir,
        collection="papers",
    )
    build_index(roots=[other], index_dir=tmp_index_dir, collection="notes")

    # Rebuild papers from scratch.
    build_index(
        roots=[shaped_corpus / "keep"],
        index_dir=tmp_index_dir,
        collection="papers",
        rebuild=True,
    )

    s = Searcher(index_dir=tmp_index_dir)
    notes_hits = s.search("stable phrase", collection="notes", limit=5)
    assert notes_hits, "rebuild of papers should not have affected notes"
    assert any(h.path.endswith("other/stable.md") for h in notes_hits)


def test_build_index_from_config_round_trips(shaped_corpus: Path, tmp_index_dir: Path) -> None:
    cfg = CollectionConfig(
        roots=[shaped_corpus],
        includes=["**/*.md"],
        excludes=["drafts/**", "archive/**"],
    )
    written = build_index_from_config(config=cfg, collection="papers", index_dir=tmp_index_dir)
    assert written > 0
    hits = Searcher(index_dir=tmp_index_dir).search("keepable", limit=10)
    paths = [h.path for h in hits]
    assert any("keep/wanted.md" in p for p in paths)
    assert not any("drafts/" in p for p in paths)


def test_load_config_reads_toml(tmp_path: Path) -> None:
    """Config file load + validate roundtrip."""
    from fnd.config import load

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""\
            [defaults]
            collection = "papers"

            [collections.papers]
            roots    = ["~/Papers"]
            includes = ["**/*.pdf"]
            excludes = ["**/draft-*"]

            [collections.notes]
            roots = ["~/Notes"]
        """),
        encoding="utf-8",
    )
    cfg = load(cfg_path)
    assert cfg.defaults.collection == "papers"
    assert "papers" in cfg.collections
    assert "notes" in cfg.collections
    papers = cfg.collection("papers")
    assert papers.includes == ["**/*.pdf"]
    assert papers.excludes == ["**/draft-*"]
