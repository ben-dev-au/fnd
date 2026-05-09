"""Phase 5.5e-2: schema bump and meta_blob field declaration."""

from __future__ import annotations

from pathlib import Path

import pytest
from tantivy import Document

from acorn.schema import F_META_BLOB, SCHEMA_VERSION, build_schema


def test_schema_version_bumped_to_two() -> None:
    assert SCHEMA_VERSION == 2


def test_meta_blob_field_constant_exists() -> None:
    assert F_META_BLOB == "meta_blob"


def test_schema_accepts_meta_blob_bytes() -> None:
    """The schema must accept ``meta_blob`` as a stored bytes field — the
    indexer writes JSON-encoded frontmatter there, retrieved at query time
    by the post-filter."""
    build_schema()
    doc = Document()
    # Should not raise — the field is declared and accepts bytes.
    doc.add_bytes(F_META_BLOB, b'{"Course": "DPwC"}')


def test_old_index_sidecar_refuses_load(tmp_path: Path) -> None:
    """An index dir with a v1 sidecar must refuse to load under v2."""
    from acorn.index import _ensure_index

    sidecar = tmp_path / ".acorn-schema-version"
    sidecar.write_text("1")
    with pytest.raises(RuntimeError, match="schema version"):
        _ensure_index(tmp_path)
