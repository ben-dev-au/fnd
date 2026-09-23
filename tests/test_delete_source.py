"""Source deletion via Ctrl+D in the source-edit form.

The actual TUI flow (Ctrl+D → DeleteSourceScreen modal → confirm)
needs Pilot for the keystroke path; this file pins the persistence
side — invoking the same write_collection round-trip the modal does
and asserting the source is gone.
"""

from __future__ import annotations

from pathlib import Path

from fnd.config import (
    CollectionConfig,
    SourceConfig,
    load,
    write_collection,
)


def test_write_collection_persists_removed_source(tmp_path: Path) -> None:
    """The modal's commit path is just CollectionConfig minus the source
    + write_collection. Verify that round-trip works."""
    cfg_path = tmp_path / "config.toml"
    a = SourceConfig(path=tmp_path / "a", includes=["**/*.md"])
    b = SourceConfig(path=tmp_path / "b", includes=["**/*.txt"], app="vscode")
    c = SourceConfig(path=tmp_path / "c", includes=["**/*.pdf"])
    write_collection(
        config_path=cfg_path,
        name="default",
        collection=CollectionConfig(sources=[a, b, c]),
    )

    cfg = load(cfg_path)
    coll = cfg.collections["default"]
    # Drop index 1 (the b source, with app=vscode override).
    del coll.sources[1]
    write_collection(config_path=cfg_path, name="default", collection=coll)

    persisted = load(cfg_path)
    sources = persisted.collections["default"].sources
    assert len(sources) == 2
    paths = [str(s.path) for s in sources]
    assert str(tmp_path / "a") in paths
    assert str(tmp_path / "c") in paths
    assert str(tmp_path / "b") not in paths


def test_delete_first_source_keeps_others_intact(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    sources = [SourceConfig(path=tmp_path / f"src{i}", includes=["**/*.md"]) for i in range(3)]
    write_collection(
        config_path=cfg_path,
        name="default",
        collection=CollectionConfig(sources=sources),
    )

    cfg = load(cfg_path)
    del cfg.collections["default"].sources[0]
    write_collection(config_path=cfg_path, name="default", collection=cfg.collections["default"])

    persisted = load(cfg_path)
    remaining = persisted.collections["default"].sources
    assert len(remaining) == 2
    assert str(remaining[0].path).endswith("src1")
    assert str(remaining[1].path).endswith("src2")


def test_delete_last_source_yields_empty_collection(tmp_path: Path) -> None:
    """Deleting the only source must leave an empty collection (not
    error, not vanish the collection itself)."""
    cfg_path = tmp_path / "config.toml"
    src = SourceConfig(path=tmp_path / "only", includes=["**/*.md"])
    write_collection(
        config_path=cfg_path,
        name="default",
        collection=CollectionConfig(sources=[src]),
    )

    cfg = load(cfg_path)
    del cfg.collections["default"].sources[0]
    write_collection(config_path=cfg_path, name="default", collection=cfg.collections["default"])

    persisted = load(cfg_path)
    assert "default" in persisted.collections
    assert persisted.collections["default"].sources == []


def test_delete_source_screen_module_imports() -> None:
    """Smoke test: catches typos in DeleteSourceScreen's class body
    without needing a full Textual mount."""
    from fnd.tui.settings_screen import DeleteSourceScreen

    assert DeleteSourceScreen is not None
