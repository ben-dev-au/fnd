"""Per-source walker."""

from __future__ import annotations

from pathlib import Path

from fnd.config import SourceConfig
from fnd.walk import walk_sources


def _touch(p: Path, body: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_walks_two_sources_with_disjoint_filetypes(tmp_path: Path) -> None:
    md_root = tmp_path / "notes"
    pdf_root = tmp_path / "course"
    _touch(md_root / "a.md")
    _touch(pdf_root / "b.pdf")
    _touch(pdf_root / "ignored.md")  # not in pdf_root's includes
    sources = [
        SourceConfig(path=md_root, includes=["**/*.md"]),
        SourceConfig(path=pdf_root, includes=["**/*.pdf"]),
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    assert paths == ["a.md", "b.pdf"]


def test_frontmatter_filter_excludes_non_matching_md(tmp_path: Path) -> None:
    """A file with no frontmatter block is not judged by a frontmatter rule.

    It used to be dropped, but only for ``.md`` — the rule was scoped by file
    kind, so the same plain text in a ``.txt`` sailed through. Scoping by
    whether the file actually carries a block makes the two consistent, and
    the direction follows the rest of the filter set: an unanswerable
    question passes rather than silently removing documents. A block that is
    present but malformed still fails closed, since that file did answer.
    """
    root = tmp_path / "notes"
    _touch(root / "in.md", "---\nCourse: DPwC\n---\nbody\n")
    _touch(root / "out.md", "---\nCourse: Algorithms\n---\nbody\n")
    _touch(root / "no_fm.md", "no frontmatter here\n")
    sources = [
        SourceConfig(
            path=root,
            includes=["**/*.md"],
            frontmatter_filter="Course == 'DPwC'",
        )
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    assert paths == ["in.md", "no_fm.md"]


def test_frontmatter_filter_only_applies_to_md(tmp_path: Path) -> None:
    """A filter on a source that contains pdf files leaves the pdfs alone —
    the filter is md-only by design (no other format has YAML frontmatter)."""
    root = tmp_path / "mixed"
    _touch(root / "a.md", "---\nCourse: Other\n---\nbody\n")
    _touch(root / "b.pdf", "%PDF-1.4 fake\n")
    sources = [
        SourceConfig(
            path=root,
            includes=["**/*.md", "**/*.pdf"],
            frontmatter_filter="Course == 'DPwC'",
        )
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    # PDF survives (filter doesn't apply); md fails the filter and is dropped.
    assert paths == ["b.pdf"]


def test_excludes_still_apply_under_filter(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    _touch(root / ".trash" / "trashed.md", "---\nCourse: DPwC\n---\nbody\n")
    _touch(root / "kept.md", "---\nCourse: DPwC\n---\nbody\n")
    sources = [
        SourceConfig(
            path=root,
            includes=["**/*.md"],
            excludes=["**/.trash/**"],
            frontmatter_filter="Course == 'DPwC'",
        )
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    assert paths == ["kept.md"]


def test_invalid_frontmatter_excludes_file(tmp_path: Path) -> None:
    """Per spec: frontmatter parse errors fail closed (filter returns False)
    so a typo in one note doesn't kill the index — but it's also not
    silently included."""
    root = tmp_path / "notes"
    _touch(root / "bad.md", "---\nfoo:\n  nested: not allowed\n---\nbody\n")
    _touch(root / "good.md", "---\nCourse: DPwC\n---\nbody\n")
    sources = [
        SourceConfig(
            path=root,
            includes=["**/*.md"],
            frontmatter_filter="Course == 'DPwC'",
        )
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    assert paths == ["good.md"]
