"""Every indexing path honours tag_sources / tag_frontmatter_keys from config.

Regression for the TUI "Update index" (run_indexer) path silently using the
hardcoded defaults and ignoring a user's configured custom frontmatter keys.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import tantivy

from fnd.config import CollectionConfig, SourceConfig
from fnd.schema import F_TAGS_FM, build_schema
from fnd.tag_query import TagFilter


def _tagged_files(index_dir: Path, tag: str) -> set[str]:
    from fnd.query import Searcher

    hits = Searcher(index_dir=index_dir).search(
        "saffron", tag_filter=TagFilter(include={"frontmatter": frozenset({tag})})
    )
    return {Path(h.path).name for h in hits}


def test_run_indexer_applies_configured_frontmatter_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_indexer sources tag settings from config internally; a configured
    Course key must become a filterable namespaced tag."""
    from fnd import config as config_mod
    from fnd.index_runner import run_indexer

    # Point config loading at a file that opts the Course key in.
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[defaults]\ntag_frontmatter_keys = ["Course"]\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "default_config_path", lambda: cfg_path)

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("---\nCourse: Algebra\n---\n\n# A\n\nsaffron\n", encoding="utf-8")
    index_dir = tmp_path / "idx"
    cc = CollectionConfig(sources=[SourceConfig(path=root)])

    async def drive() -> None:
        async for _ in run_indexer(
            config=cc, collection="default", index_dir=index_dir, rebuild=True
        ):
            pass

    asyncio.run(drive())

    # The configured key produced the namespaced tag; without threading this
    # would be empty.
    assert _tagged_files(index_dir, "course/algebra") == {"a.md"}
    assert _tagged_files(index_dir, "course") == {"a.md"}


def test_run_indexer_defaults_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config file → bare defaults, and plain tags still index."""
    from fnd import config as config_mod
    from fnd.index_runner import run_indexer

    missing = tmp_path / "nope.toml"
    monkeypatch.setattr(config_mod, "default_config_path", lambda: missing)

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text(
        "---\ntags: [recipe]\nCourse: Algebra\n---\n\n# A\n\nsaffron\n", encoding="utf-8"
    )
    index_dir = tmp_path / "idx"
    cc = CollectionConfig(sources=[SourceConfig(path=root)])

    async def drive() -> None:
        async for _ in run_indexer(
            config=cc, collection="default", index_dir=index_dir, rebuild=True
        ):
            pass

    asyncio.run(drive())

    index = tantivy.Index.open(str(index_dir))
    s = index.searcher()
    q = tantivy.Query.term_query(build_schema(), F_TAGS_FM, "recipe")
    assert len(s.search(q, 5).hits) == 1  # plain tag indexed
    # Course NOT opted in by default → no namespaced tag.
    assert _tagged_files(index_dir, "course") == set()
