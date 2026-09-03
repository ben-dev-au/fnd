"""Index-time filters as the walker applies them, defaults and overrides included."""

from __future__ import annotations

from pathlib import Path

import pytest

import fnd.frontmatter
from fnd.config import Config, DefaultFilters, SourceConfig, SourceFilters
from fnd.walk import walk_sources


def _write(root: Path, rel: str, body: str = "x") -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _sources(
    root: Path, *, defaults: DefaultFilters | None = None, **source_kw: object
) -> list[SourceConfig]:
    """Sources resolved through a real Config, as the app always builds them."""
    cfg = Config.model_validate(
        {
            "defaults": {"filters": (defaults or DefaultFilters()).model_dump()},
            "collections": {"c": {"sources": [{"path": str(root), **source_kw}]}},
        }
    )
    return cfg.collections["c"].sources


def _names(root: Path, *, defaults: DefaultFilters | None = None, **kw: object) -> set[str]:
    return {p.name for p in walk_sources(sources=_sources(root, defaults=defaults, **kw))}


class TestIgnoreFiles:
    def test_gitignore_excludes_by_default(self, tmp_path: Path) -> None:
        _write(tmp_path, ".gitignore", "*.md\n")
        _write(tmp_path, "a.md")
        _write(tmp_path, "b.txt")
        assert _names(tmp_path) == {"b.txt"}

    def test_nested_negation_reinstates(self, tmp_path: Path) -> None:
        _write(tmp_path, ".gitignore", "*.md\n")
        _write(tmp_path, "sub/.gitignore", "!keep.md\n")
        _write(tmp_path, "sub/keep.md")
        _write(tmp_path, "sub/drop.md")
        assert _names(tmp_path) == {"keep.md"}

    def test_ignored_directory_is_not_descended(self, tmp_path: Path) -> None:
        _write(tmp_path, ".gitignore", "build/\n")
        _write(tmp_path, "build/out.md")
        _write(tmp_path, "keep.md")
        assert _names(tmp_path) == {"keep.md"}

    def test_fndignore_is_honoured(self, tmp_path: Path) -> None:
        _write(tmp_path, ".fndignore", "*.md\n")
        _write(tmp_path, "a.md")
        _write(tmp_path, "b.txt")
        assert _names(tmp_path) == {"b.txt"}

    def test_gitignore_can_be_turned_off_globally(self, tmp_path: Path) -> None:
        _write(tmp_path, ".gitignore", "*.md\n")
        _write(tmp_path, "a.md")
        off = DefaultFilters(respect_gitignore=False)
        assert _names(tmp_path, defaults=off) == {"a.md"}

    def test_gitignore_can_be_turned_off_per_source(self, tmp_path: Path) -> None:
        _write(tmp_path, ".gitignore", "*.md\n")
        _write(tmp_path, "a.md")
        override = SourceFilters(respect_gitignore=False).model_dump(exclude_none=True)
        assert _names(tmp_path, filters=override) == {"a.md"}

    def test_fndignore_survives_gitignore_being_off(self, tmp_path: Path) -> None:
        _write(tmp_path, ".gitignore", "*.txt\n")
        _write(tmp_path, ".fndignore", "*.md\n")
        _write(tmp_path, "a.md")
        _write(tmp_path, "b.txt")
        off = DefaultFilters(respect_gitignore=False)
        assert _names(tmp_path, defaults=off) == {"b.txt"}


class TestRepositoryBoundary:
    """An outer .gitignore stops at a nested repository root, as git's does.

    Found against the real corpus: cloned assignments sit inside a course
    folder whose .gitignore names them, and git keeps them because the nested
    repo is its own scope.
    """

    def test_outer_gitignore_does_not_reach_into_a_nested_repo(self, tmp_path: Path) -> None:
        _write(tmp_path, ".gitignore", "sub/keep.md\n")
        (tmp_path / "sub" / ".git").mkdir(parents=True)
        _write(tmp_path, "sub/keep.md")
        _write(tmp_path, "outside.md")
        assert _names(tmp_path) == {"keep.md", "outside.md"}

    def test_outer_gitignore_still_applies_without_a_nested_repo(self, tmp_path: Path) -> None:
        """Negative control: the same tree minus the .git is excluded."""
        _write(tmp_path, ".gitignore", "sub/drop.md\n")
        _write(tmp_path, "sub/drop.md")
        _write(tmp_path, "outside.md")
        assert _names(tmp_path) == {"outside.md"}

    def test_a_nested_repo_uses_its_own_gitignore(self, tmp_path: Path) -> None:
        (tmp_path / "sub" / ".git").mkdir(parents=True)
        _write(tmp_path, "sub/.gitignore", "*.md\n")
        _write(tmp_path, "sub/drop.md")
        _write(tmp_path, "outside.md")
        assert _names(tmp_path) == {"outside.md"}


class TestTagExclusion:
    """``exclude_tags`` reads OS tags only.

    Frontmatter tags would mean opening every candidate during enumeration —
    the shape of the scan stall — and would move cloud fetches out of the
    reported per-file phase. Excluding on a YAML tag is a frontmatter filter.
    """

    def test_frontmatter_tags_do_not_gate_the_walk(self, tmp_path: Path) -> None:
        _write(tmp_path, "tagged.md", "---\ntags: [no_index]\n---\nbody\n")
        assert _names(tmp_path) == {"tagged.md"}

    def test_a_frontmatter_filter_excludes_on_a_yaml_tag(self, tmp_path: Path) -> None:
        _write(tmp_path, "drop.md", "---\ntags: [no_index]\n---\nbody\n")
        _write(tmp_path, "keep.md", "---\ntags: [keep]\n---\nbody\n")
        spec = DefaultFilters(frontmatter="NOT ('no_index' in tags)")
        assert _names(tmp_path, defaults=spec) == {"keep.md"}

    def test_untagged_notes_survive_a_negated_tag_filter(self, tmp_path: Path) -> None:
        """``NOT (x in tags)`` is true on a note with no tags: — keep it."""
        _write(tmp_path, "plain.md", "no frontmatter here\n")
        spec = DefaultFilters(frontmatter="NOT ('no_index' in tags)")
        assert _names(tmp_path, defaults=spec) == {"plain.md"}

    def test_the_walk_does_not_read_frontmatter_for_the_default_filters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard against reintroducing the enumeration-time file open."""
        _write(tmp_path, "a.md", "---\ntags: [x]\n---\n")
        reads: list[Path] = []
        real = fnd.frontmatter.read_frontmatter_from_file
        monkeypatch.setattr(
            "fnd.file_facts.read_frontmatter_from_file",
            lambda p: (reads.append(p), real(p))[1],
        )
        assert _names(tmp_path) == {"a.md"}
        assert reads == [], f"enumeration opened files it did not need: {reads}"


class TestStructuredDimensions:
    def test_kinds_restricts_file_types(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md")
        _write(tmp_path, "b.txt")
        assert _names(tmp_path, defaults=DefaultFilters(kinds=["md"])) == {"a.md"}

    def test_max_size_drops_large_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "small.md", "x")
        _write(tmp_path, "big.md", "x" * 500)
        assert _names(tmp_path, defaults=DefaultFilters(max_size=100)) == {"small.md"}

    def test_expression_applies_to_every_kind(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "xx")
        _write(tmp_path, "b.txt", "x")
        spec = DefaultFilters(expression="file.size <= 1")
        assert _names(tmp_path, defaults=spec) == {"b.txt"}


class TestFrontmatterScope:
    def test_legacy_filter_still_only_touches_notes(self, tmp_path: Path) -> None:
        """Non-note kinds must pass a frontmatter predicate untouched."""
        _write(tmp_path, "match.md", "---\nCourse: DPwC\n---\n")
        _write(tmp_path, "other.md", "---\nCourse: Other\n---\n")
        _write(tmp_path, "paper.pdf", "%PDF-1.4\n")
        names = _names(tmp_path, frontmatter_filter="Course == 'DPwC'")
        assert names == {"match.md", "paper.pdf"}

    def test_filters_frontmatter_field_behaves_the_same(self, tmp_path: Path) -> None:
        _write(tmp_path, "match.md", "---\nCourse: DPwC\n---\n")
        _write(tmp_path, "paper.pdf", "%PDF-1.4\n")
        spec = DefaultFilters(frontmatter="Course == 'DPwC'")
        assert _names(tmp_path, defaults=spec) == {"match.md", "paper.pdf"}


class TestInheritance:
    def test_source_override_wins_over_default(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md")
        _write(tmp_path, "b.txt")
        override = SourceFilters(kinds=["txt"]).model_dump(exclude_none=True)
        got = _names(tmp_path, defaults=DefaultFilters(kinds=["md"]), filters=override)
        assert got == {"b.txt"}

    def test_unset_fields_still_inherit(self, tmp_path: Path) -> None:
        """Overriding one field must not reset the others to their defaults."""
        _write(tmp_path, ".gitignore", "*.txt\n")
        _write(tmp_path, "a.md")
        _write(tmp_path, "b.txt")
        override = SourceFilters(max_size=10_000).model_dump(exclude_none=True)
        assert _names(tmp_path, filters=override) == {"a.md"}
