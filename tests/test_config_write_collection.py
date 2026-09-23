"""write_collection round-trips a CollectionConfig through the renderer."""

from __future__ import annotations

import textwrap
from pathlib import Path

from fnd.config import (
    CollectionConfig,
    SourceConfig,
    delete_collection,
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
    # ``**/*.md`` is not the ``md`` kind (that also covers ``.markdown``),
    # so it stays a glob rather than being folded into ``filters.kinds``.
    assert out.collection("notes").sources[0].includes == ["**/*.md"]


def test_a_write_keeps_the_notes_block(tmp_path: Path) -> None:
    """Comments outside the notes block are regenerated; that is the trade the
    canonical renderer makes."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "# >>> notes: kept verbatim when fnd rewrites this file\n# mine\n# <<< notes\n"
        '[[collections.papers.sources]]\npath = "/tmp/papers"\n',
        encoding="utf-8",
    )
    write_collection(
        config_path=cfg_path,
        name="notes",
        collection=CollectionConfig(sources=[SourceConfig(path=Path("/tmp/notes"))]),
    )
    text = cfg_path.read_text(encoding="utf-8")
    assert "# mine" in text
    assert "papers" in load(cfg_path).collections
    assert "notes" in load(cfg_path).collections


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


def test_delete_collection_removes_table(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"

            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    delete_collection(config_path=cfg_path, name="notes")
    out = load(cfg_path)
    assert "papers" in out.collections
    assert "notes" not in out.collections


def test_delete_missing_collection_is_idempotent(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    # Should not raise.
    delete_collection(config_path=cfg_path, name="absent")
    assert cfg_path.read_text(encoding="utf-8") == ""
