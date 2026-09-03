"""The cascade's fuzzy pass must honour the Filters pane's field qualifiers.

``_PrefixingSearcher`` re-attaches the filter prefix only on ``_raw_hits`` and
``_filtered_raw_hits``; ``_fuzzy_pass`` reaches the inner searcher through
``__getattr__`` and re-derives filters from the query string it was handed,
which carries no prefix.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fnd.cascade import cascade_search
from fnd.index import build_index
from fnd.query import Searcher
from fnd.tui.search_controller import _PrefixingSearcher


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
    searcher = _PrefixingSearcher(Searcher(index_dir=tmp_index_dir), prefix="kind:md")

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

    searcher = _PrefixingSearcher(Searcher(index_dir=tmp_index_dir), prefix="mtime:week")
    hits = cascade_search(
        searcher,  # type: ignore[arg-type]
        query="glimer",
        threshold=50,
        limit=50,
        collection="c",
    )

    names = {Path(h.path).name for h in hits}
    assert names == {"fresh.md"}, f"mtime filter leaked: {sorted(names)}"
