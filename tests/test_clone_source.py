"""Phase 5: ``clone_source`` deep-copies a source into another collection.

The clone preserves every field — including the Phase 2 app refs —
and is independent of the original (no shared mutable state). The TOML
round-trip is exercised via ``Config.load`` after the write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import (
    CollectionConfig,
    SourceConfig,
    clone_source,
    load,
    write_collection,
)


def _setup_two_collections(tmp_path: Path) -> Path:
    """Two collections — `src_coll` has one fully-populated source,
    `dst_coll` is empty."""
    cfg_path = tmp_path / "config.toml"
    src = SourceConfig(
        path=tmp_path / "notes",
        includes=["**/*.md", "**/*.txt"],
        excludes=["**/.git/**", "**/drafts/**"],
        follow_symlinks=True,
        frontmatter_filter="type == 'note'",
        app="obsidian",
        app_for={"md": "obsidian", "pdf": "skim"},
        app_params={"vault": "MyVault"},
    )
    write_collection(
        config_path=cfg_path,
        name="src_coll",
        collection=CollectionConfig(sources=[src]),
    )
    write_collection(
        config_path=cfg_path,
        name="dst_coll",
        collection=CollectionConfig(sources=[]),
    )
    return cfg_path


def test_clone_appends_to_target_collection(tmp_path: Path) -> None:
    cfg_path = _setup_two_collections(tmp_path)
    new_idx = clone_source(
        config_path=cfg_path,
        source_collection="src_coll",
        source_index=0,
        target_collection="dst_coll",
    )
    # Returned index is 0-based — the new entry's position in the
    # target collection's sources list.
    assert new_idx == 0
    cfg = load(cfg_path)
    assert len(cfg.collections["dst_coll"].sources) == 1


def test_clone_preserves_every_field_including_app_refs(tmp_path: Path) -> None:
    cfg_path = _setup_two_collections(tmp_path)
    clone_source(
        config_path=cfg_path,
        source_collection="src_coll",
        source_index=0,
        target_collection="dst_coll",
    )
    cfg = load(cfg_path)
    cloned = cfg.collections["dst_coll"].sources[0]
    original = cfg.collections["src_coll"].sources[0]

    assert cloned.path == original.path
    assert cloned.includes == original.includes
    assert cloned.excludes == original.excludes
    assert cloned.follow_symlinks == original.follow_symlinks
    assert cloned.frontmatter_filter == original.frontmatter_filter
    assert cloned.app == original.app
    assert cloned.app_for == original.app_for
    assert cloned.app_params == original.app_params


def test_clone_is_independent_of_original(tmp_path: Path) -> None:
    """Mutating the cloned source must not reach the original.

    A direct deep-copy via Pydantic guarantees this; the in-memory
    SourceConfig is frozen-by-convention but its dict / list children
    are mutable, so a naive shallow copy WOULD alias.
    """
    cfg_path = _setup_two_collections(tmp_path)
    clone_source(
        config_path=cfg_path,
        source_collection="src_coll",
        source_index=0,
        target_collection="dst_coll",
    )
    cfg = load(cfg_path)
    cloned = cfg.collections["dst_coll"].sources[0]
    original = cfg.collections["src_coll"].sources[0]

    cloned.includes.append("**/*.pdf")
    assert "**/*.pdf" not in original.includes
    cloned.app_params["vault"] = "OtherVault"
    assert original.app_params["vault"] == "MyVault"


def test_clone_rejects_same_collection(tmp_path: Path) -> None:
    cfg_path = _setup_two_collections(tmp_path)
    with pytest.raises(ValueError, match=r"differ"):
        clone_source(
            config_path=cfg_path,
            source_collection="src_coll",
            source_index=0,
            target_collection="src_coll",
        )


def test_clone_rejects_unknown_source_collection(tmp_path: Path) -> None:
    cfg_path = _setup_two_collections(tmp_path)
    with pytest.raises(KeyError, match=r"ghost"):
        clone_source(
            config_path=cfg_path,
            source_collection="ghost",
            source_index=0,
            target_collection="dst_coll",
        )


def test_clone_rejects_unknown_target_collection(tmp_path: Path) -> None:
    cfg_path = _setup_two_collections(tmp_path)
    with pytest.raises(KeyError, match=r"ghost"):
        clone_source(
            config_path=cfg_path,
            source_collection="src_coll",
            source_index=0,
            target_collection="ghost",
        )


def test_clone_rejects_out_of_range_index(tmp_path: Path) -> None:
    cfg_path = _setup_two_collections(tmp_path)
    with pytest.raises(IndexError, match=r"out of range"):
        clone_source(
            config_path=cfg_path,
            source_collection="src_coll",
            source_index=99,
            target_collection="dst_coll",
        )
