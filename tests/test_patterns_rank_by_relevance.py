"""Regex and infix or leading wildcards rank by BM25 over the terms they match.

A regex query scores every hit 1.0, so on its own its order is the order the
index holds documents in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec, word_matches
from fnd.query import Searcher

FILLER = " ".join(f"filler{i}" for i in range(80))


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    root = tmp_path / "notes"
    root.mkdir()
    # Indexed first, so a constant score would list them first.
    for n in range(3):
        (root / f"a{n}_passing.md").write_text(f"# Aside {n}\n\n{FILLER} crypto sha256 {FILLER}\n")
    (root / "z_dense.md").write_text(
        "# Crypto\n\ncrypto crypto crypto keys, sha256 and sha512 hashes, crypto again.\n"
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return Searcher(index_dir=tmp_index_dir)


@pytest.mark.parametrize("query", ["/crypt(o|ography)/", "cr*to", "*rypto", "/sha[0-9]+/"])
def test_the_dense_file_ranks_first(searcher: Searcher, query: str) -> None:
    names = [Path(g.path).name for g in search_layered(searcher, query=query, limit=10)]
    assert names[0] == "z_dense.md", names
    assert len(names) == 4, "ranking must not drop a match"


def test_a_regex_with_no_literal_prefix_still_matches(searcher: Searcher) -> None:
    names = {Path(g.path).name for g in search_layered(searcher, query="/.*rypto/", limit=10)}
    assert len(names) == 4


@pytest.mark.parametrize(("query", "word"), [("/sha[0-9]+/", "SHA512"), ("*rypto", "Crypto")])
def test_highlighting_matches_what_the_search_matched(query: str, word: str) -> None:
    assert word_matches(word, MatchSpec.from_query(query))
