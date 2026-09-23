"""The Index-filters row said "Needs a reindex to take effect".

Reindex reads as Rebuild (the expensive one that empties the collection
first), and an Update already does the job: it walks with the current filters,
so a file that now matches is added, and one that no longer matches is pruned.
The tag rows are the opposite case and say so: tags are read when a file is
indexed, and an Update skips files that have not changed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from fnd.config import CollectionConfig, SourceConfig, SourceFilters
from fnd.index import build_index_from_config
from fnd.query import Searcher


def _collection(root: Path, *, max_size: int | None) -> CollectionConfig:
    return CollectionConfig(
        sources=[SourceConfig(path=root, filters=SourceFilters(max_size=max_size))],
    )


def _indexed(index_dir: Path) -> set[str]:
    searcher = Searcher(index_dir=index_dir)
    hits = searcher.search("saffron", limit=50, collection="notes")
    return {Path(h.path).name for h in hits}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "small.md").write_text("saffron\n", encoding="utf-8")
    (root / "big.md").write_text("saffron " * 400, encoding="utf-8")
    return root


def test_an_update_admits_what_a_widened_filter_now_matches(
    corpus: Path, tmp_index_dir: Path
) -> None:
    build_index_from_config(
        config=_collection(corpus, max_size=100),
        collection="notes",
        index_dir=tmp_index_dir,
    )
    assert _indexed(tmp_index_dir) == {"small.md"}

    build_index_from_config(
        config=_collection(corpus, max_size=None),
        collection="notes",
        index_dir=tmp_index_dir,
    )

    assert _indexed(tmp_index_dir) == {"small.md", "big.md"}, "no rebuild was needed"


def test_an_update_drops_what_a_narrowed_filter_no_longer_matches(
    corpus: Path, tmp_index_dir: Path
) -> None:
    build_index_from_config(
        config=_collection(corpus, max_size=None),
        collection="notes",
        index_dir=tmp_index_dir,
    )
    assert _indexed(tmp_index_dir) == {"small.md", "big.md"}

    build_index_from_config(
        config=_collection(corpus, max_size=100),
        collection="notes",
        index_dir=tmp_index_dir,
    )

    assert _indexed(tmp_index_dir) == {"small.md"}, "the pruning pass runs on an update too"


def _description(row_id: str) -> str:
    from fnd.tui.menu import section_items

    app = cast("Any", SimpleNamespace(_config=None))
    for section in ("filters", "index_filters", "preferences"):
        for item in section_items(app, section):
            if item.id == row_id:
                return item.description
    raise AssertionError(f"{row_id} is on no screen")


@pytest.mark.parametrize(
    ("row_id", "must_say", "must_not_say"),
    [
        ("filters.browse", "Update index", "Needs a reindex"),
        ("filters.tag_frontmatter_keys", "Rebuild index", "Needs a reindex to take effect"),
        ("filters.tag_sources", "Rebuild index", "needs a reindex"),
    ],
)
def test_each_row_names_the_command_it_needs(row_id: str, must_say: str, must_not_say: str) -> None:
    """Two commands, two different answers; "reindex" named neither."""
    description = _description(row_id)
    assert must_say in description, description
    assert must_not_say not in description, description
