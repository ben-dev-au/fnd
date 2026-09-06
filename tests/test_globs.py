"""One glob language: what the walker admits, ``~~`` must agree with."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filter_dsl import compile_filter
from fnd.globs import GlobSet, PathGlob
from fnd.walk import walk

# Shapes the real config never uses — the corpus diff is green over its two
# include shapes either way, so a gate built on it proves nothing.
GLOBS = [
    "**/*.md",
    "*.md",
    "**/*",
    "*",
    "notes/**",
    "**/notes/**",
    "notes/*.md",
    "**/a.md",
    "a.md",
    "**/[ab].md",
    "[ab].md",
    "?.md",
    "**/?.md",
    "sub/**/*.md",
    "**/**/*.md",
    "**/deep/**",
    "deep",
    "**/*.txt",
    "notes/a.md",
    "**/x y.md",
]

CORPUS = [
    "a.md",
    "b.txt",
    "c.pdf",
    "x y.md",
    "two.dots.md",
    "notes/a.md",
    "notes/b.txt",
    "notes/deep/a.md",
    "sub/a.md",
    "sub/deep/nested/a.md",
    "deep/a.md",
]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("globs")
    for rel in CORPUS:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    return root


@pytest.mark.parametrize("glob", GLOBS)
def test_the_walker_and_the_dsl_admit_the_same_files(glob: str, corpus: Path) -> None:
    """They were separate fnmatch call sites and disagreed on every ``**/``
    pattern; nothing but a shared matcher keeps them from drifting again."""
    walked = {p.relative_to(corpus).as_posix() for p in walk(roots=[corpus], includes=[glob])}
    predicate = compile_filter(f"file.path ~~ '{glob}'")
    every = {p.relative_to(corpus).as_posix() for p in walk(roots=[corpus])}
    by_rule = {rel for rel in every if predicate({"file.path": rel})}
    assert walked == by_rule


class TestTheLanguage:
    """The three places this differs from the ``fnmatch`` it replaced."""

    def test_a_leading_star_star_matches_at_the_root(self) -> None:
        assert PathGlob("**/README.md").matches("README.md")
        assert PathGlob("**/README.md").matches("d/README.md")

    def test_a_directory_glob_reaches_a_root_level_directory(self) -> None:
        assert PathGlob("**/build/**").matches("build/c.md")
        assert PathGlob("**/build/**").matches("x/build/c.md")

    def test_a_star_stops_at_a_separator(self) -> None:
        assert PathGlob("*.md").matches("a.md")
        assert not PathGlob("*.md").matches("sub/a.md")

    def test_a_star_star_segment_spans_zero_directories(self) -> None:
        assert PathGlob("sub/**/*.md").matches("sub/a.md")
        assert PathGlob("sub/**/*.md").matches("sub/deep/a.md")

    def test_a_pattern_the_engine_cannot_compile_never_matches(self) -> None:
        """git tolerates an unusable pattern; aborting an index run does not."""
        assert not PathGlob("[z-a].md").matches("a.md")

    def test_matching_is_case_sensitive(self) -> None:
        assert not PathGlob("*.md").matches("A.MD")


class TestGlobSet:
    def test_an_empty_set_is_falsey_because_it_means_everything(self) -> None:
        assert not GlobSet.parse(None)
        assert not GlobSet.parse([])

    def test_any_member_admits(self) -> None:
        both = GlobSet.parse(["**/*.md", "**/*.pdf"])
        assert both.matches("a.md")
        assert both.matches("d/a.pdf")
        assert not both.matches("a.txt")
