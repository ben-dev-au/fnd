"""Enumerating the tags present in the active collections, with file counts."""

from __future__ import annotations

from pathlib import Path

import tantivy

from fnd.schema import F_TAGS_FM, F_TAGS_OS, build_schema
from fnd.tag_catalogue import TagCount, tag_catalogue


def _index(tmp_path: Path, rows: list[tuple[str, str, list[str], list[str], int]]) -> tantivy.Index:
    """rows: (parent_id, collection, fm_tags, os_tags, n_chunks)."""
    index = tantivy.Index(build_schema(), path=str(tmp_path))
    w = index.writer(15_000_000)
    for pid, coll, fm, os_, n in rows:
        for seq in range(n):
            d = tantivy.Document()
            d.add_text("parent_id", pid)
            d.add_text("collection", coll)
            d.add_text("body", f"body {seq}")
            for t in fm:
                d.add_text(F_TAGS_FM, t)
            for t in os_:
                d.add_text(F_TAGS_OS, t)
            w.add_document(d)
    w.commit()
    index.reload()
    return index


def test_lists_tags_per_source(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        [
            ("a.md", "vault", ["recipe"], ["red"], 1),
            ("b.md", "vault", ["travel"], [], 1),
        ],
    )
    got = tag_catalogue(index, collections=["vault"])
    assert {t.value for t in got["frontmatter"]} == {"recipe", "travel"}
    assert {t.value for t in got["os"]} == {"red"}


def test_counts_files_not_chunks(tmp_path: Path) -> None:
    """A 40-chunk file tagged 'report' is one file, not forty."""
    index = _index(
        tmp_path,
        [("big.pdf", "vault", ["report"], [], 40), ("small.md", "vault", ["report"], [], 2)],
    )
    got = tag_catalogue(index, collections=["vault"])
    assert got["frontmatter"] == [TagCount(value="report", files=2)]


def test_parent_count_is_a_union_not_a_sum(tmp_path: Path) -> None:
    """Ancestors are expanded at index time, so a file tagged with two
    siblings contributes to the parent once."""
    index = _index(
        tmp_path,
        [
            ("both.md", "vault", ["project", "project/alpha", "project/beta"], [], 1),
            ("alpha.md", "vault", ["project", "project/alpha"], [], 1),
            ("bare.md", "vault", ["project"], [], 1),
        ],
    )
    counts = {t.value: t.files for t in tag_catalogue(index, collections=["vault"])["frontmatter"]}
    assert counts["project"] == 3
    assert counts["project/alpha"] == 2
    assert counts["project/beta"] == 1


def test_scoped_to_active_collections(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        [
            ("a.md", "vault", ["recipe"], [], 1),
            ("b.md", "work", ["report"], [], 1),
        ],
    )
    got = tag_catalogue(index, collections=["vault"])
    assert {t.value for t in got["frontmatter"]} == {"recipe"}
    got_all = tag_catalogue(index, collections=["vault", "work"])
    assert {t.value for t in got_all["frontmatter"]} == {"recipe", "report"}


def test_no_collection_scope_covers_everything(tmp_path: Path) -> None:
    index = _index(
        tmp_path, [("a.md", "vault", ["recipe"], [], 1), ("b.md", "work", ["report"], [], 1)]
    )
    got = tag_catalogue(index, collections=[])
    assert {t.value for t in got["frontmatter"]} == {"recipe", "report"}


def test_sorted_by_count_then_name(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        [
            ("a.md", "vault", ["common", "zebra"], [], 1),
            ("b.md", "vault", ["common"], [], 1),
            ("c.md", "vault", ["common", "apple"], [], 1),
        ],
    )
    got = [t.value for t in tag_catalogue(index, collections=["vault"])["frontmatter"]]
    assert got[0] == "common"
    assert got[1:] == ["apple", "zebra"]


def test_sources_filter_limits_the_query(tmp_path: Path) -> None:
    index = _index(tmp_path, [("a.md", "vault", ["recipe"], ["red"], 1)])
    got = tag_catalogue(index, collections=["vault"], sources=["frontmatter"])
    assert "os" not in got
    assert {t.value for t in got["frontmatter"]} == {"recipe"}


def test_empty_index_yields_empty_lists(tmp_path: Path) -> None:
    index = _index(tmp_path, [])
    got = tag_catalogue(index, collections=["vault"])
    assert got == {"frontmatter": [], "os": []}


def test_limit_caps_each_source(tmp_path: Path) -> None:
    rows = [(f"f{i}.md", "vault", [f"t{i}"], [], 1) for i in range(30)]
    index = _index(tmp_path, rows)
    got = tag_catalogue(index, collections=["vault"], limit=5)
    assert len(got["frontmatter"]) == 5


def test_scoped_to_a_query(tmp_path: Path) -> None:
    """Tags reflect the files matching the query, not the whole collection."""
    index = _index(
        tmp_path,
        [
            ("a.md", "vault", ["recipe"], [], 1),
            ("b.md", "vault", ["travel"], [], 1),
        ],
    )
    q = index.parse_query('"a.md"', ["parent_id"])
    got = tag_catalogue(index, collections=["vault"], query=q)
    assert {t.value for t in got["frontmatter"]} == {"recipe"}


def test_query_and_collection_scope_combine(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        [
            ("a.md", "vault", ["recipe"], [], 1),
            ("a.md", "work", ["report"], [], 1),
        ],
    )
    q = index.parse_query('"a.md"', ["parent_id"])
    got = tag_catalogue(index, collections=["vault"], query=q)
    assert {t.value for t in got["frontmatter"]} == {"recipe"}


def test_no_query_falls_back_to_collection_scope(tmp_path: Path) -> None:
    index = _index(tmp_path, [("a.md", "vault", ["recipe"], [], 1)])
    got = tag_catalogue(index, collections=["vault"], query=None)
    assert {t.value for t in got["frontmatter"]} == {"recipe"}
