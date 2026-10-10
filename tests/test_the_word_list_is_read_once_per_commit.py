"""A leading wildcard reads the whole word list once per index commit, not once
per reload: the TUI reloads before every query."""

from __future__ import annotations

from pathlib import Path

from fnd import query_resolvers
from fnd.index import build_index
from fnd.layered import search_layered
from fnd.query import Searcher


def test_a_reload_without_a_commit_keeps_the_word_list(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("# A\n\nnormalization here.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    searcher = Searcher(index_dir=tmp_index_dir)

    search_layered(searcher, query="*ization", limit=10)
    first = query_resolvers._dictionary
    searcher.reload()
    search_layered(searcher, query="*ization", limit=10)
    assert query_resolvers._dictionary is first

    (root / "b.md").write_text("# B\n\nserialization there.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    searcher.reload()
    names = {
        Path(g.path).name for g in search_layered(searcher, query='"*ization there"', limit=10)
    }
    assert names == {"b.md"}
    assert query_resolvers._dictionary is not first
