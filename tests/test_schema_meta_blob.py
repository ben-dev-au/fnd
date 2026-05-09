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


def test_force_rebuild_wipes_stale_index_dir(tmp_path: Path) -> None:
    """``force=True`` is the rebuild path: it must clear leftover Tantivy
    files so a new index can be opened under the current schema. A stale
    sidecar alone isn't enough — Tantivy stores the schema in
    ``meta.json`` too, and the constructor refuses to open a mismatched
    dir.

    Verifies that a stale dir with a non-matching ``meta.json`` doesn't
    cause Tantivy to raise; the rebuild path clears the dir then opens
    a fresh index. (Tantivy regenerates its own ``meta.json`` after the
    wipe — that's expected.)
    """
    from acorn.index import _ensure_index

    sidecar = tmp_path / ".acorn-schema-version"
    sidecar.write_text("1")
    # Simulate leftover artefacts from a v1 index. ``meta.json`` with
    # a mismatched schema is what triggered the original bug.
    (tmp_path / "meta.json").write_text(
        '{"schema": [{"name": "old_field", "type": "text"}]}',
        encoding="utf-8",
    )
    (tmp_path / "0001.fast").write_bytes(b"\x00\x01\x02")
    nested = tmp_path / "subdir-from-old-segment"
    nested.mkdir()
    (nested / "x").write_text("y")

    # Should not raise — the rebuild path wipes and reinitialises.
    _ensure_index(tmp_path, force=True)

    # Sidecar now matches current version.
    assert sidecar.read_text().strip() == str(SCHEMA_VERSION)
    # The stale subtree we created is gone (Tantivy creates its own
    # fresh files; the user's leftover dirs are not preserved).
    assert not nested.exists()
