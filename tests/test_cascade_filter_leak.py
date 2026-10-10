"""The cascade's fuzzy pass must honour the Filters pane's field qualifiers.

``FilteredSearcher`` passes its clauses only through ``_candidates``;
``_fuzzy_pass`` reaches the inner searcher through ``__getattr__`` and builds its own query, so it must read ``filter_clauses``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fnd.cascade import cascade_search
from fnd.index import build_index
from fnd.query import FilteredSearcher, Searcher


def _index_two_kinds(tmp_path: Path, index_dir: Path) -> Path:
    """One ``.md`` and one ``.txt`` sharing a term a 1-edit typo reaches."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "note.md").write_text("# Note\nthe glimmer pattern is shown here.\n", encoding="utf-8")
    (root / "plain.txt").write_text("the glimmer pattern is shown here.\n", encoding="utf-8")
    build_index(roots=[root], index_dir=index_dir, collection="c")
    return index_dir


def test_fuzzy_pass_honours_kind_filter(tmp_path: Path, tmp_index_dir: Path) -> None:
    """``kind:md`` in the prefix must not be dropped by the fuzzy pass."""
    _index_two_kinds(tmp_path, tmp_index_dir)
    searcher = FilteredSearcher(Searcher(index_dir=tmp_index_dir), clauses=["kind:md"])

    hits = cascade_search(
        searcher,  # type: ignore[arg-type]
        query="glimer",
        threshold=50,
        limit=50,
        collection="c",
    )

    assert hits, "fuzzy pass should still reach the 1-edit typo"
    assert {h.kind for h in hits} == {"md"}, f"kind filter leaked: {sorted(h.kind for h in hits)}"


def test_unprefixed_cascade_still_returns_both(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Negative control: without a prefix both kinds are expected."""
    _index_two_kinds(tmp_path, tmp_index_dir)
    searcher = Searcher(index_dir=tmp_index_dir)

    hits = cascade_search(searcher, query="glimer", threshold=50, limit=50, collection="c")

    assert {h.kind for h in hits} == {"md", "txt"}


def test_fuzzy_pass_honours_date_filter(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The same leak on a range qualifier, which compiles differently to a term."""
    root = tmp_path / "corpus"
    root.mkdir()
    fresh = root / "fresh.md"
    stale = root / "stale.md"
    for f in (fresh, stale):
        f.write_text("the glimmer pattern is shown here.\n", encoding="utf-8")
    old = time.time() - 400 * 86400
    os.utime(stale, (old, old))
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    searcher = FilteredSearcher(Searcher(index_dir=tmp_index_dir), clauses=["mtime:week"])
    hits = cascade_search(
        searcher,  # type: ignore[arg-type]
        query="glimer",
        threshold=50,
        limit=50,
        collection="c",
    )

    names = {Path(h.path).name for h in hits}
    assert names == {"fresh.md"}, f"mtime filter leaked: {sorted(names)}"


def test_fuzzy_pass_honours_two_filters_at_once(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Two clauses joined for the parser must not read as none to the pass.

    `extract_filters` will not lift a `field:value` adjacent to a boolean
    operator, so re-deriving filters from `kind:md AND mtime:week glimer`
    yielded nothing and BOTH filters leaked. One filter alone kept working,
    which is why every test above passed while a real pane setting two of them
    returned files that violate one.
    """
    root = tmp_path / "corpus"
    root.mkdir()
    keep = root / "keep.md"
    old_md = root / "old.md"
    other = root / "keep.txt"
    for f in (keep, old_md, other):
        f.write_text("the glimmer pattern is shown here.\n", encoding="utf-8")
    old = time.time() - 400 * 86400
    os.utime(old_md, (old, old))
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    searcher = FilteredSearcher(
        Searcher(index_dir=tmp_index_dir), clauses=["kind:md", "mtime:week"]
    )
    hits = cascade_search(
        searcher,  # type: ignore[arg-type]
        query="glimer",
        threshold=50,
        limit=50,
        collection="c",
    )

    names = {Path(h.path).name for h in hits}
    assert names == {"keep.md"}, f"a conjunction leaked: {sorted(names)}"


def test_a_nested_filter_keeps_the_synonym_pass(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The fuzzy pass stands down for ``(kind:md AND kind:md)``; the synonym pass keeps it."""
    from fnd.synonyms import load_default_synonyms

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "login.md").write_text("# Login\nwe use single sign-on here.\n", encoding="utf-8")
    (root / "login.txt").write_text("we use single sign-on here.\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    query = "(kind:md AND kind:md) AND sso"

    hits = cascade_search(
        Searcher(index_dir=tmp_index_dir),
        query=query,
        threshold=50,
        limit=50,
        collection="c",
        synonyms=load_default_synonyms(),
    )

    assert {Path(h.path).name for h in hits} == {"login.md"}


def test_highlighting_drops_fuzzy_where_the_fuzzy_pass_stands_down() -> None:
    from fnd.matching import MatchSpec

    assert MatchSpec.from_query("(kind:md AND kind:md) AND glimmer").fuzzy_per_stem == ()
    assert MatchSpec.from_query("kind:md glimmer").fuzzy_per_stem != ()
