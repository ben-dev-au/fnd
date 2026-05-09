"""Schema-migration helpers — detection + rebuild prompt."""

from __future__ import annotations

from pathlib import Path

from acorn.migrate import SchemaStatus, check_schema_status


def test_empty_dir_returns_empty(tmp_path: Path) -> None:
    """No sidecar at all = no index here yet (e.g. first run)."""
    assert check_schema_status(tmp_path) == (SchemaStatus.EMPTY, None)


def test_current_version_returns_ready(tmp_path: Path) -> None:
    from acorn.schema import SCHEMA_VERSION

    (tmp_path / ".acorn-schema-version").write_text(str(SCHEMA_VERSION))
    assert check_schema_status(tmp_path) == (SchemaStatus.READY, None)


def test_old_version_returns_stale(tmp_path: Path) -> None:
    (tmp_path / ".acorn-schema-version").write_text("1")
    status, existing = check_schema_status(tmp_path)
    assert status is SchemaStatus.STALE
    assert existing == "1"


def test_garbage_sidecar_returns_stale(tmp_path: Path) -> None:
    """A sidecar with non-numeric content is treated as stale (will be
    overwritten on rebuild). Don't assume any specific text format."""
    (tmp_path / ".acorn-schema-version").write_text("garbage\n")
    status, existing = check_schema_status(tmp_path)
    assert status is SchemaStatus.STALE
    assert existing == "garbage"
