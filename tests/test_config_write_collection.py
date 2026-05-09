"""Phase 5.5e-3: write_collection round-trips a CollectionConfig via tomlkit."""

from __future__ import annotations

import textwrap
from pathlib import Path

from acorn.config import (
    CollectionConfig,
    SourceConfig,
    load,
    write_collection,
)


def test_write_creates_collection_in_empty_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    cc = CollectionConfig(
        sources=[
            SourceConfig(path=Path("/tmp/notes"), includes=["**/*.md"]),
        ]
    )
    write_collection(config_path=cfg_path, name="notes", collection=cc)
    out = load(cfg_path)
    assert out.collection("notes").sources[0].path == Path("/tmp/notes")
    assert out.collection("notes").sources[0].includes == ["**/*.md"]


def test_write_preserves_user_comments(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            # I love this config.
            [defaults]
            # important note
            collection = "notes"

            [[collections.papers.sources]]
            path = "/tmp/papers"
        """),
        encoding="utf-8",
    )
    cc = CollectionConfig(
        sources=[
            SourceConfig(path=Path("/tmp/notes"), includes=["**/*.md"]),
        ]
    )
    write_collection(config_path=cfg_path, name="notes", collection=cc)
    text = cfg_path.read_text(encoding="utf-8")
    assert "# I love this config." in text
    assert "# important note" in text
    # papers collection still present
    assert "/tmp/papers" in text
    # notes collection added
    assert "/tmp/notes" in text


def test_write_replaces_existing_collection(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/old"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    cc = CollectionConfig(
        sources=[
            SourceConfig(path=Path("/tmp/new"), includes=["**/*.txt"]),
            SourceConfig(path=Path("/tmp/extra"), includes=["**/*.pdf"]),
        ]
    )
    write_collection(config_path=cfg_path, name="notes", collection=cc)
    out = load(cfg_path)
    paths = [s.path for s in out.collection("notes").sources]
    assert paths == [Path("/tmp/new"), Path("/tmp/extra")]


def test_write_with_frontmatter_filter(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    cc = CollectionConfig(
        sources=[
            SourceConfig(
                path=Path("/tmp/notes"),
                includes=["**/*.md"],
                frontmatter_filter="Course == 'DPwC'",
            )
        ]
    )
    write_collection(config_path=cfg_path, name="notes", collection=cc)
    out = load(cfg_path)
    s = out.collection("notes").sources[0]
    assert s.frontmatter_filter == "Course == 'DPwC'"
