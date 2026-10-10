"""Query-time post-filter using compile_filter on meta_blob."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.filter_dsl import FilterError
from fnd.index import build_index_from_config
from fnd.query import Searcher


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def filter_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    _touch(
        notes / "website.md",
        "---\nProject: Website\nstatus: active\n---\n# A\npenguin sandwich here\n",
    )
    _touch(
        notes / "budget.md",
        "---\nProject: Budget\nstatus: active\n---\n# B\npenguin sandwich also\n",
    )
    _touch(
        notes / "archived.md",
        "---\nProject: Website\nstatus: archived\n---\n# C\npenguin sandwich third\n",
    )
    _touch(notes / "untagged.md", "# D\npenguin sandwich plain\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    return tmp_index_dir


def test_meta_filter_narrows_to_matching_md(filter_corpus: Path) -> None:
    s = Searcher(index_dir=filter_corpus)
    hits = s.search(
        "penguin sandwich",
        limit=10,
        collection="notes",
        metadata_filter="Project == 'Website' AND status != 'archived'",
    )
    paths = {Path(h.path).name for h in hits}
    assert "website.md" in paths
    assert "budget.md" not in paths
    assert "archived.md" not in paths
    assert "untagged.md" not in paths  # strict null


def test_meta_filter_empty_string_is_invalid(filter_corpus: Path) -> None:
    """Defensive: an empty filter string must NOT compile — callers should
    pass None for "no filter". An empty string is a bug on the caller side
    and we want a clear error."""
    s = Searcher(index_dir=filter_corpus)
    with pytest.raises(FilterError):
        s.search("penguin", limit=5, metadata_filter="")


def test_meta_filter_invalid_raises(filter_corpus: Path) -> None:
    s = Searcher(index_dir=filter_corpus)
    with pytest.raises(FilterError):
        s.search("penguin", limit=5, metadata_filter="Project ==")


def test_meta_filter_passes_through_when_none(filter_corpus: Path) -> None:
    """metadata_filter=None means no post-filter; same hits as without it."""
    s = Searcher(index_dir=filter_corpus)
    baseline = s.search("penguin sandwich", limit=10, collection="notes")
    with_none = s.search("penguin sandwich", limit=10, collection="notes", metadata_filter=None)
    assert {h.parent_id for h in baseline} == {h.parent_id for h in with_none}


def test_meta_filter_grouped_dedup_still_one_hit_per_file(
    filter_corpus: Path,
) -> None:
    """search_grouped's per-file dedup applies AFTER the post-filter."""
    s = Searcher(index_dir=filter_corpus)
    groups = s.search_grouped(
        "penguin sandwich",
        limit=10,
        collection="notes",
        metadata_filter="Project == 'Website'",
    )
    paths = {Path(g.path).name for g in groups}
    # website.md and archived.md both match Project == 'Website'; status filter
    # not applied here, so both surface.
    assert paths == {"website.md", "archived.md"}


def test_meta_filter_oversample_still_returns_limit_when_filter_strict(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Build many md files, most failing the filter. The post-filter must
    oversample-and-retry until ``limit`` survivors emerge."""
    notes = tmp_path / "notes"
    # 50 notes, but only every 10th matches Project == 'Website'.
    for i in range(50):
        project = "Website" if i % 10 == 0 else "Other"
        _touch(
            notes / f"n{i:02}.md",
            f"---\nProject: {project}\n---\n# {i}\npenguin sandwich {i}\n",
        )
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)

    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search(
        "penguin sandwich",
        limit=5,
        collection="notes",
        metadata_filter="Project == 'Website'",
    )
    # 5 of the 50 match the filter; we asked for limit=5 — must get all 5.
    assert len(hits) == 5
    for h in hits:
        # Sanity: every returned hit's path corresponds to a 0/10/20/30/40 file.
        idx = int(Path(h.path).stem.lstrip("n"))
        assert idx % 10 == 0
