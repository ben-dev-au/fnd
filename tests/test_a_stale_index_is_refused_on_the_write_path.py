"""A previous-version index must be refused by the writer, not appended into.

The read side already prompts a rebuild (test_cli_migrate). This locks the write
side: an incremental update opens the index with ``force=False`` (index_runner
line 886), so a schema bump must make that raise rather than mix new-shape
documents into old-shape segments. A rebuild (``force=True``) wipes and proceeds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import _ensure_index
from fnd.migrate import SchemaStatus, check_schema_status
from fnd.schema import SCHEMA_VERSION

_SIDECAR = ".fnd-schema-version"


def _stale_index(tmp_path: Path) -> Path:
    """A real current-version index whose sidecar is then set one version back."""
    index_dir = tmp_path / "index"
    _ensure_index(index_dir, force=False)  # creates segments + current sidecar
    (index_dir / _SIDECAR).write_text(str(SCHEMA_VERSION - 1), encoding="utf-8")
    return index_dir


def test_an_update_refuses_a_stale_index(tmp_path: Path) -> None:
    index_dir = _stale_index(tmp_path)

    with pytest.raises(RuntimeError, match="schema version"):
        _ensure_index(index_dir, force=False)


def test_check_schema_status_reports_stale(tmp_path: Path) -> None:
    index_dir = _stale_index(tmp_path)

    status, existing = check_schema_status(index_dir)

    assert status is SchemaStatus.STALE
    assert existing == str(SCHEMA_VERSION - 1)


def test_a_rebuild_wipes_and_re_establishes_the_sidecar(tmp_path: Path) -> None:
    index_dir = _stale_index(tmp_path)

    _ensure_index(index_dir, force=True)

    assert (index_dir / _SIDECAR).read_text(encoding="utf-8").strip() == str(SCHEMA_VERSION)


def test_a_current_index_opens_without_a_rebuild(tmp_path: Path) -> None:
    """The control: a matching sidecar must open on the write path untouched, or
    the guard is refusing everything, not just the stale case."""
    index_dir = tmp_path / "index"
    _ensure_index(index_dir, force=False)

    _ensure_index(index_dir, force=False)  # must not raise

    assert (index_dir / _SIDECAR).read_text(encoding="utf-8").strip() == str(SCHEMA_VERSION)
