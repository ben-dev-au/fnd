"""Phase 3 (Settings UX redesign) — cross-section search tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from acorn.config import CollectionConfig, Config, SourceConfig
from acorn.index import build_index
from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _seed_config(fixtures_dir: Path) -> Config:
    """A Config with a single ``default`` collection so the Collections
    section walker yields a per-collection row labelled ``default``."""
    return Config(
        collections={
            "default": CollectionConfig(sources=[SourceConfig(path=fixtures_dir)]),
        }
    )


@pytest.mark.asyncio
async def test_walk_all_sections_includes_every_leaf(built_index: Path, fixtures_dir: Path) -> None:
    """Spec: Search behaviour › Index — walker covers Preferences,
    Collections, Keybindings, and root-level actions."""
    from acorn.tui.menu import KIND_HEADER, walk_all_sections

    app = AcornApp(index_dir=built_index, config=_seed_config(fixtures_dir))
    async with app.run_test():
        all_items = list(walk_all_sections(app))
        labels = {item.label for _path, item in all_items}
        # Preferences leaves:
        assert "Result limit" in labels
        assert "Default collection" in labels
        # Collections section includes the per-collection drill row.
        assert "default" in labels
        # Keybindings keys (sample):
        assert any(item.label == "Quit" for _, item in all_items)
        # Root action:
        assert "Open config file in editor" in labels
        # No headers leak through.
        assert not any(item.kind == KIND_HEADER for _, item in all_items)
