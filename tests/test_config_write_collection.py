"""Phase 5.5e-3: write_collection round-trips a CollectionConfig via tomlkit."""

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
    # ``includes = ["**/*.md"]`` is stored as the filter it states.
    assert out.collection("notes").sources[0].filters.kinds == ["md"]


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
    # papers collection still present (a verbatim forward-slash literal in the
    # pre-written config — preserved as-is by tomlkit on every OS)
    assert "/tmp/papers" in text
    # notes collection added — assert via reload, not a POSIX-literal substring:
    # a Path("/tmp/notes") stringifies with backslashes on Windows and tomlkit
    # escapes them in the written TOML.
    reloaded = load(cfg_path)
    assert Path("/tmp/notes") in [s.path for s in reloaded.collection("notes").sources]


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
            # important
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
    text = cfg_path.read_text(encoding="utf-8")
    assert "# important" in text


def test_delete_missing_collection_is_idempotent(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    # Should not raise.
    delete_collection(config_path=cfg_path, name="absent")
    assert cfg_path.read_text(encoding="utf-8") == ""
