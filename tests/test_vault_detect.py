"""Phase 2: Obsidian vault auto-detection.

``fnd.apps.detect_obsidian_vault(path)`` walks up from ``path`` looking
for a ``.obsidian/`` directory. Used by the Settings TUI to pre-fill
``app_params.vault`` when the user picks Obsidian as a source's app.
"""

from __future__ import annotations

from pathlib import Path

from fnd.apps import detect_obsidian_vault


def test_detects_when_source_is_vault_root(tmp_path: Path) -> None:
    vault = tmp_path / "MyVault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    assert detect_obsidian_vault(vault) == "MyVault"


def test_detects_when_source_is_subdirectory_of_vault(tmp_path: Path) -> None:
    vault = tmp_path / "MyVault"
    notes = vault / "daily" / "2026"
    notes.mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    assert detect_obsidian_vault(notes) == "MyVault"


def test_returns_none_when_no_vault_in_ancestors(tmp_path: Path) -> None:
    plain = tmp_path / "plain_dir" / "nested"
    plain.mkdir(parents=True)
    assert detect_obsidian_vault(plain) is None


def test_returns_first_vault_when_nested(tmp_path: Path) -> None:
    """When nested vaults exist (rare but possible), the nearest ancestor
    wins. Reflects what Obsidian itself would open."""
    outer = tmp_path / "Outer"
    inner = outer / "Inner"
    inner.mkdir(parents=True)
    (outer / ".obsidian").mkdir()
    (inner / ".obsidian").mkdir()
    deep = inner / "subdir"
    deep.mkdir()
    assert detect_obsidian_vault(deep) == "Inner"


def test_accepts_file_path_and_walks_from_parent(tmp_path: Path) -> None:
    vault = tmp_path / "MyVault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    note = vault / "note.md"
    note.write_text("# Hi\n")
    assert detect_obsidian_vault(note) == "MyVault"
