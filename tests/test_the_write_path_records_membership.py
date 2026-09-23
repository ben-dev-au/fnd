"""The indexer writes each file's membership token, not just its collection."""

from __future__ import annotations

from pathlib import Path

import tantivy

from fnd.index import build_index
from fnd.schema import F_COLLECTION, F_MEMBERSHIP, build_schema, membership_token


def test_build_index_records_a_membership_token(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\n\nrisotto\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    index = tantivy.Index.open(str(tmp_index_dir))
    index.reload()
    searcher = index.searcher()
    schema = build_schema()

    hits = searcher.search(tantivy.Query.term_query(schema, F_COLLECTION, "c"), limit=10).hits
    assert hits, "the collection term did not match the written doc"
    doc = searcher.doc(hits[0][1])
    # Ad-hoc index has no configured source, so the pair is (collection, "").
    assert doc.get_first(F_MEMBERSHIP) == membership_token("c", "")
