"""The advice on an empty index moves forward: never a step already taken."""

from __future__ import annotations

from pathlib import Path

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.migrate import _next_step


def test_no_collections_points_at_the_wizard() -> None:
    """`fnd tui` opens on Add Collection when there is nothing to search."""
    said = _next_step(Config())
    assert "fnd tui" in said
    assert "Add Collection" in said


def test_a_configured_collection_is_told_to_build_it(tmp_path: Path) -> None:
    """The control this function already had: never advise a step taken."""
    config = Config(collections={"notes": CollectionConfig(sources=[SourceConfig(path=tmp_path)])})
    said = _next_step(config)
    assert "reindex notes" in said, said
    assert "collection add" not in said


def test_a_name_needing_quotes_is_advised_pasteably(tmp_path: Path) -> None:
    config = Config(
        collections={"Q3 [growth=5%]": CollectionConfig(sources=[SourceConfig(path=tmp_path)])}
    )
    assert "`fnd collection reindex 'Q3 [growth=5%]'`" in _next_step(config)
