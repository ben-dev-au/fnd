"""v8 adds created/inode-ctime/tag fields and round-trips them."""

from __future__ import annotations

from pathlib import Path

import tantivy

from fnd.schema import (
    DEFAULT_SEARCH_FIELDS,
    F_CREATED,
    F_INODE_CTIME,
    F_TAGS_FM,
    F_TAGS_OS,
    SCHEMA_VERSION,
    TAG_FIELD_BY_SOURCE,
    build_schema,
)


def test_schema_version_at_or_past_8() -> None:
    """>= so a later bump doesn't falsely fail; the field round-trips below
    are the real guard that v8's shape survived."""
    assert SCHEMA_VERSION >= 8


def test_new_fields_round_trip(tmp_path: Path) -> None:
    schema = build_schema()
    index = tantivy.Index(schema, path=str(tmp_path))
    writer = index.writer(15_000_000)
    doc = tantivy.Document()
    doc.add_text("parent_id", "p1")
    doc.add_text("collection", "c1")
    doc.add_text("path", "/tmp/a.md")
    doc.add_text("body", "hello")
    doc.add_unsigned(F_CREATED, 1_700_000_000)
    doc.add_unsigned(F_INODE_CTIME, 1_700_000_500)
    doc.add_text(F_TAGS_FM, "recipe")
    doc.add_text(F_TAGS_FM, "project/alpha")
    doc.add_text(F_TAGS_OS, "red")
    writer.add_document(doc)
    writer.commit()
    index.reload()

    searcher = index.searcher()
    hits = searcher.search(index.parse_query("hello", ["body"]), 10).hits
    assert len(hits) == 1
    stored = searcher.doc(hits[0][1])
    assert stored.get_first(F_CREATED) == 1_700_000_000
    assert stored.get_first(F_INODE_CTIME) == 1_700_000_500
    assert set(stored.get_all(F_TAGS_FM)) == {"recipe", "project/alpha"}
    assert set(stored.get_all(F_TAGS_OS)) == {"red"}


def test_tag_fields_are_exact_match_not_stemmed(tmp_path: Path) -> None:
    """The raw tokenizer must keep slashes and spaces intact."""
    schema = build_schema()
    index = tantivy.Index(schema, path=str(tmp_path))
    writer = index.writer(15_000_000)
    doc = tantivy.Document()
    doc.add_text("parent_id", "p1")
    doc.add_text("body", "hello")
    doc.add_text(F_TAGS_FM, "two words/nested")
    writer.add_document(doc)
    writer.commit()
    index.reload()
    searcher = index.searcher()
    q = tantivy.Query.term_query(schema, F_TAGS_FM, "two words/nested")
    assert len(searcher.search(q, 10).hits) == 1


def test_tags_are_not_in_default_search_fields() -> None:
    """A bare keyword search must not match on tags."""
    assert F_TAGS_FM not in DEFAULT_SEARCH_FIELDS
    assert F_TAGS_OS not in DEFAULT_SEARCH_FIELDS


def test_tag_field_mapping_is_defined_once() -> None:
    """Writer and reader share one table so they cannot disagree."""
    assert TAG_FIELD_BY_SOURCE == {"frontmatter": F_TAGS_FM, "os": F_TAGS_OS}
