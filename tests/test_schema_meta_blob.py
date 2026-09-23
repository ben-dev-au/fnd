"""Schema bump and meta_blob field declaration."""

from __future__ import annotations

from pathlib import Path

import pytest
from tantivy import Document

from fnd.schema import F_META_BLOB, SCHEMA_VERSION, build_schema


def test_schema_version_is_at_least_two() -> None:
    """``meta_blob`` was introduced in v2; later bumps must keep it."""
    assert SCHEMA_VERSION >= 2


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
    from fnd.index import _ensure_index

    sidecar = tmp_path / ".fnd-schema-version"
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
    from fnd.index import _ensure_index

    sidecar = tmp_path / ".fnd-schema-version"
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


def _build_legacy_v1_index(index_dir: Path) -> None:
    """Build a real Tantivy index with a schema missing ``meta_blob`` (the
    v1 shape). Stamp the *current* sidecar on top so the file system
    looks like the bug-stuck state: sidecar says v2, segments say v1.

    Used by both the ``_ensure_index`` recovery test and the
    ``check_schema_status`` openable-trial test.
    """
    from tantivy import Index, SchemaBuilder

    sb = SchemaBuilder()
    sb.add_text_field("body", stored=False, tokenizer_name="default")
    schema = sb.build()
    Index(schema, path=str(index_dir))
    # Pretend a prior force-rebuild bumped the sidecar but crashed before
    # Tantivy got new segments — the exact stuck state in the field.
    (index_dir / ".fnd-schema-version").write_text(str(SCHEMA_VERSION))


def test_force_rebuild_recovers_from_inconsistent_sidecar_and_meta_json(
    tmp_path: Path,
) -> None:
    """The bug from the field: a half-completed rebuild bumped the sidecar
    to the current version but Tantivy's meta.json still has v1 schema.
    Subsequent calls with ``force=True`` must wipe and recreate, not
    propagate Tantivy's ``Schema error`` ValueError."""
    from fnd.index import _ensure_index

    _build_legacy_v1_index(tmp_path)
    # Sanity: opening with the current schema raises ValueError.
    from tantivy import Index

    with pytest.raises(ValueError, match=r"(?i)schema"):
        Index(build_schema(), path=str(tmp_path))
    # _ensure_index(force=True) recovers cleanly.
    _ensure_index(tmp_path, force=True)
    # And re-opening succeeds without issue (the dir is now consistent).
    Index(build_schema(), path=str(tmp_path))


def test_open_without_force_on_inconsistent_dir_gives_clear_error(
    tmp_path: Path,
) -> None:
    """Without ``force=True``, the same inconsistent state surfaces as a
    RuntimeError pointing the user at the rebuild command — not Tantivy's
    cryptic ``Schema error``."""
    from fnd.index import _ensure_index

    _build_legacy_v1_index(tmp_path)
    with pytest.raises(RuntimeError, match=r"inconsistent|rebuild"):
        _ensure_index(tmp_path)


def test_check_schema_status_detects_inconsistent_dir(tmp_path: Path) -> None:
    """``check_schema_status`` is the migrate helper's first signal. When
    the sidecar matches but Tantivy disagrees, it must return STALE so
    the migrate prompt fires (instead of trusting the sidecar and
    blowing up later in the TUI)."""
    from fnd.migrate import SchemaStatus, check_schema_status

    _build_legacy_v1_index(tmp_path)
    status, version = check_schema_status(tmp_path)
    assert status is SchemaStatus.STALE
    assert version == "inconsistent"
