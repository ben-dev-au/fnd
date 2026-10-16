"""Semantic search is off until installed; every collection takes part unless it opts out."""

from __future__ import annotations

from pathlib import Path

from fnd.config import CollectionConfig, Config, Defaults
from fnd.config_render import render_config


def test_semantic_search_defaults_off() -> None:
    """A config without the keys searches on words alone and shows embedding progress."""
    d = Defaults()
    assert d.semantic_search is False
    assert d.show_embedding_progress is True


def test_a_collection_takes_part_by_default() -> None:
    """Collections are opted in unless they say otherwise."""
    assert CollectionConfig().semantic is True


def test_the_rendered_config_names_the_new_keys() -> None:
    """The written config lists both keys under a Semantic search heading."""
    text = render_config(Config())
    head = text.index("# Semantic search")
    assert text.index("semantic_search = false") > head
    assert text.index("show_embedding_progress = true") > head


def test_an_opted_out_collection_is_written_and_read_back(tmp_path: Path) -> None:
    """`semantic = false` survives a write and a load."""
    from fnd import config as conf

    path = tmp_path / "config.toml"
    path.write_text(render_config(Config(collections={"books": CollectionConfig(semantic=False)})))
    assert conf.load(path).collections["books"].semantic is False
