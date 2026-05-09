"""Phase 5.5e-1: SourceConfig + multi-source collection schema."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from acorn.config import SourceConfig, load


def _write_config(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_new_sources_shape_loads(tmp_path: Path) -> None:
    p = _write_config(
        tmp_path / "c.toml",
        """
        [[collections.coursework.sources]]
        path     = "~/Notes"
        includes = ["**/*.md"]
        excludes = ["**/.trash/**"]
        frontmatter_filter = "Course == 'DPwC'"

        [[collections.coursework.sources]]
        path     = "~/Course/DPwC"
        includes = ["**/*.pdf"]
    """,
    )
    cfg = load(p)
    coursework = cfg.collection("coursework")
    assert len(coursework.sources) == 2
    assert isinstance(coursework.sources[0], SourceConfig)
    assert coursework.sources[0].includes == ["**/*.md"]
    assert coursework.sources[0].frontmatter_filter == "Course == 'DPwC'"
    assert coursework.sources[1].includes == ["**/*.pdf"]
    assert coursework.sources[1].frontmatter_filter is None


def test_legacy_flat_shape_normalised_to_one_source(tmp_path: Path) -> None:
    """The old `roots = [...]` shape still loads; loader rewrites it as a
    single implicit source with no frontmatter_filter."""
    p = _write_config(
        tmp_path / "c.toml",
        """
        [collections.papers]
        roots    = ["~/Documents/Papers"]
        includes = ["**/*.pdf"]
        excludes = ["**/Archive/**"]
    """,
    )
    cfg = load(p)
    papers = cfg.collection("papers")
    assert len(papers.sources) == 1
    s = papers.sources[0]
    assert s.path == Path("~/Documents/Papers").expanduser()
    assert s.includes == ["**/*.pdf"]
    assert s.excludes == ["**/Archive/**"]
    assert s.frontmatter_filter is None


def test_mixing_sources_and_roots_raises(tmp_path: Path) -> None:
    p = _write_config(
        tmp_path / "c.toml",
        """
        [collections.bad]
        roots = ["~/x"]
        [[collections.bad.sources]]
        path = "~/y"
    """,
    )
    with pytest.raises(ValidationError, match="mixes legacy 'roots' with 'sources'"):
        load(p)


def test_invalid_filter_dsl_raises_at_load(tmp_path: Path) -> None:
    p = _write_config(
        tmp_path / "c.toml",
        """
        [[collections.x.sources]]
        path = "~/x"
        frontmatter_filter = "Course =="
    """,
    )
    with pytest.raises(ValidationError) as exc:
        load(p)
    msg = str(exc.value)
    assert "frontmatter_filter" in msg
    assert "col" in msg


def test_paths_tilde_expanded(tmp_path: Path) -> None:
    p = _write_config(
        tmp_path / "c.toml",
        """
        [[collections.x.sources]]
        path = "~/Notes"
    """,
    )
    cfg = load(p)
    s = cfg.collection("x").sources[0]
    assert "~" not in str(s.path)


def test_default_includes_excludes_empty_when_omitted(tmp_path: Path) -> None:
    p = _write_config(
        tmp_path / "c.toml",
        """
        [[collections.x.sources]]
        path = "~/x"
    """,
    )
    s = load(p).collection("x").sources[0]
    assert s.includes == []
    assert s.excludes == []
    assert s.follow_symlinks is False
