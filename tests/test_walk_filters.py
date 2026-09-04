"""Index-time filters as the walker applies them, defaults and overrides included."""

from __future__ import annotations

import datetime as dt
import os
import sys
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
    """``exclude_tags`` reads every tag source, so the default is not macOS-only."""

    def test_a_yaml_tag_excludes_like_a_finder_tag_would(self, tmp_path: Path) -> None:
        """The only cross-platform way to say "keep this out" — no Finder needed."""
        _write(tmp_path, "tagged.md", "---\ntags: [no_index]\n---\nbody\n")
        _write(tmp_path, "plain.md", "---\ntags: [keep]\n---\nbody\n")
        assert _names(tmp_path) == {"plain.md"}

    def test_a_tag_the_filter_does_not_name_is_kept(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "---\ntags: [draft]\n---\nbody\n")
        assert _names(tmp_path) == {"a.md"}

    def test_clearing_the_default_indexes_the_tagged_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "tagged.md", "---\ntags: [no_index]\n---\nbody\n")
        assert _names(tmp_path, defaults=DefaultFilters(exclude_tags=[])) == {"tagged.md"}

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

    def test_a_filter_that_asks_nothing_of_content_opens_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading content is opt-in. Measured 0.40s -> 1.92s over 4,439 notes,
        so a dimension that acquires a content read by accident is not free."""
        _write(tmp_path, "a.md", "---\ntags: [x]\n---\n")
        reads: list[Path] = []
        real = fnd.frontmatter.read_frontmatter_from_file
        monkeypatch.setattr(
            "fnd.file_facts.read_frontmatter_from_file",
            lambda p: (reads.append(p), real(p))[1],
        )
        spec = DefaultFilters(exclude_tags=[], kinds=["md"], max_size=1_000)
        assert _names(tmp_path, defaults=spec) == {"a.md"}
        assert reads == [], f"enumeration opened files it did not need: {reads}"

    def test_the_tag_default_does_read_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The negative control for the test above: it can tell the two apart."""
        _write(tmp_path, "a.md", "---\ntags: [x]\n---\n")
        reads: list[Path] = []
        real = fnd.frontmatter.read_frontmatter_from_file
        monkeypatch.setattr(
            "fnd.file_facts.read_frontmatter_from_file",
            lambda p: (reads.append(p), real(p))[1],
        )
        assert _names(tmp_path) == {"a.md"}
        assert reads, "the no_index default must read a note's YAML tags"


class TestStructuredDimensions:
    def test_kinds_restricts_file_types(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md")
        _write(tmp_path, "b.txt")
        assert _names(tmp_path, defaults=DefaultFilters(kinds=["md"])) == {"a.md"}

    def test_max_size_drops_large_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "small.md", "x")
        _write(tmp_path, "big.md", "x" * 500)
        assert _names(tmp_path, defaults=DefaultFilters(max_size=100)) == {"small.md"}

    def test_min_size_drops_small_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "stub.md", "x")
        _write(tmp_path, "real.md", "x" * 50)
        assert _names(tmp_path, defaults=DefaultFilters(min_size=10)) == {"real.md"}

    def test_modified_after_drops_older_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "fresh.md")
        stale = _write(tmp_path, "stale.md")
        old_ts = (dt.datetime(2020, 1, 1, tzinfo=dt.UTC)).timestamp()
        os.utime(stale, (old_ts, old_ts))
        spec = DefaultFilters(modified_after=dt.date(2023, 1, 1))
        assert _names(tmp_path, defaults=spec) == {"fresh.md"}

    def test_modified_before_drops_newer_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "fresh.md")
        stale = _write(tmp_path, "stale.md")
        old_ts = (dt.datetime(2020, 1, 1, tzinfo=dt.UTC)).timestamp()
        os.utime(stale, (old_ts, old_ts))
        spec = DefaultFilters(modified_before=dt.date(2023, 1, 1))
        assert _names(tmp_path, defaults=spec) == {"stale.md"}

    def test_an_unknown_creation_date_keeps_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ext4 without statx reports no birth time; dropping would index nothing."""
        from fnd.fsmeta import FileTimes

        _write(tmp_path, "a.md")
        monkeypatch.setattr(
            "fnd.file_facts.read_file_times",
            lambda _p: FileTimes(mtime=1_700_000_000, created=0, inode_changed=1),
        )
        spec = DefaultFilters(created_after=dt.date(2024, 1, 1))
        assert _names(tmp_path, defaults=spec) == {"a.md"}

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


class TestSymlinkedRoot:
    """Facts must measure against the root ``walk`` actually yields from.

    ``walk`` resolves the root, so a symlinked ancestor made ``file.path``
    fall back to the absolute path and any rule using it stop matching.
    macOS /tmp and /var are themselves symlinks, so this needs no opt-in.
    """

    def test_file_path_is_relative_under_a_symlinked_ancestor(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        (real / "notes").mkdir(parents=True)
        _write(real, "notes/todo.md")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        spec = DefaultFilters(expression="file.path == 'todo.md'")
        got = {p.name for p in walk_sources(sources=_sources(link / "notes", defaults=spec))}
        assert got == {"todo.md"}

    def test_hidden_is_not_confused_by_a_dot_in_an_ancestor(self, tmp_path: Path) -> None:
        """An absolute-path fallback would read a dotted ancestor as hidden.

        The source root itself is a real directory — a symlinked *root* is
        refused outright unless ``follow_symlinks`` is set, so the ancestor
        is what carries the link.
        """
        real = tmp_path / ".hidden-parent"
        (real / "notes").mkdir(parents=True)
        _write(real, "notes/plain.md")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        spec = DefaultFilters(expression="file.hidden == false")
        got = {p.name for p in walk_sources(sources=_sources(link / "notes", defaults=spec))}
        assert got == {"plain.md"}


class TestOverrideToNothing:
    """A source can opt out of a global exclusion, not just change it.

    ``-`` in a per-source row stores an empty override. Treating that as
    "untouched" reinstated the global value, so the opt-out was a no-op.
    """

    def test_an_emptied_expression_exempts_the_source(self, tmp_path: Path) -> None:
        _write(tmp_path, "big.md", "x" * 500)
        globals_ = DefaultFilters(expression="file.size < 10")
        assert _names(tmp_path, defaults=globals_) == set()

        override = {"expression": ""}
        assert _names(tmp_path, defaults=globals_, filters=override) == {"big.md"}

    def test_an_emptied_kind_list_exempts_the_source(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md")
        _write(tmp_path, "b.txt")
        globals_ = DefaultFilters(kinds=["md"])
        assert _names(tmp_path, defaults=globals_) == {"a.md"}

        override = {"kinds": []}
        assert _names(tmp_path, defaults=globals_, filters=override) == {"a.md", "b.txt"}


class TestTypeGlobAbsorption:
    """``includes = ["**/*.md"]`` and ``filters.kinds = ["md"]`` say one thing."""

    def test_a_type_glob_becomes_a_kind(self, tmp_path: Path) -> None:
        src = _sources(tmp_path, includes=["**/*.md"])[0]
        assert src.includes == []
        assert src.filters is not None
        assert src.filters.kinds == ["md"]

    def test_a_custom_glob_is_left_where_it_is(self, tmp_path: Path) -> None:
        """Only a plain file-type glob has a kinds equivalent."""
        src = _sources(tmp_path, includes=["**/*.md", ".obsidian/**"])[0]
        assert src.includes == [".obsidian/**"]
        assert src.filters is not None
        assert src.filters.kinds == ["md"]

    def test_an_explicit_kinds_override_is_never_overwritten(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "collections": {
                    "c": {
                        "sources": [
                            {
                                "path": str(tmp_path),
                                "includes": ["**/*.md"],
                                "filters": {"kinds": ["pdf"]},
                            }
                        ]
                    }
                }
            }
        )
        src = cfg.collections["c"].sources[0]
        assert src.filters is not None
        assert src.filters.kinds == ["pdf"]
        assert src.includes == ["**/*.md"], "the globs must not vanish silently"

    def test_absorbing_twice_changes_nothing(self, tmp_path: Path) -> None:
        """``_normalise_sources`` re-runs on every model_validate."""
        once = _sources(tmp_path, includes=["**/*.md"])[0]
        twice = (
            Config.model_validate(
                {"collections": {"c": {"sources": [once.model_dump(exclude_none=True)]}}}
            )
            .collections["c"]
            .sources[0]
        )
        assert twice.includes == once.includes
        assert twice.filters is not None
        assert once.filters is not None
        assert twice.filters.kinds == once.filters.kinds

    def test_the_walk_yields_the_same_files_either_way(self, tmp_path: Path) -> None:
        """Measured identical across all 14 real sources; pinned here."""
        _write(tmp_path, "a.md")
        _write(tmp_path, "b.pdf")
        _write(tmp_path, "c.txt")
        globbed = {p.name for p in walk_sources(sources=_sources(tmp_path, includes=["**/*.md"]))}
        kinded = {
            p.name
            for p in walk_sources(sources=_sources(tmp_path, defaults=DefaultFilters(kinds=["md"])))
        }
        assert globbed == kinded == {"a.md"}


class TestTagsAreNotConflated:
    """A system tag and a note's ``tags:`` entry sharing a word are different
    statements about a file, and each is filterable on its own."""

    @staticmethod
    def _corpus(root: Path) -> None:
        import plistlib
        import subprocess

        _write(root, "yaml_draft.md", "---\ntags: [draft]\n---\nbody\n")
        _write(root, "os_draft.md", "plain note\n")
        _write(root, "clean.md", "nothing\n")
        blob = plistlib.dumps(["draft"], fmt=plistlib.FMT_BINARY).hex()
        subprocess.run(
            [
                "xattr",
                "-wx",
                "com.apple.metadata:_kMDItemUserTags",
                blob,
                str(root / "os_draft.md"),
            ],
            check=True,
        )

    @pytest.mark.skipif(sys.platform != "darwin", reason="system tags are macOS-only")
    @pytest.mark.parametrize(
        ("selection", "kept"),
        [
            ({"frontmatter": ["draft"]}, {"clean.md", "os_draft.md"}),
            ({"os": ["draft"]}, {"clean.md", "yaml_draft.md"}),
            (["draft"], {"clean.md"}),
        ],
    )
    def test_excluding_one_source_leaves_the_other(
        self, tmp_path: Path, selection: object, kept: set[str]
    ) -> None:
        self._corpus(tmp_path)
        spec = DefaultFilters(exclude_tags=selection)  # type: ignore[arg-type]
        assert _names(tmp_path, defaults=spec) == kept

    @pytest.mark.skipif(sys.platform != "darwin", reason="system tags are macOS-only")
    def test_including_one_source_admits_only_it(self, tmp_path: Path) -> None:
        self._corpus(tmp_path)
        spec = DefaultFilters(include_tags={"os": ["draft"]}, exclude_tags=[])
        assert _names(tmp_path, defaults=spec) == {"os_draft.md"}

    def test_a_bare_list_still_means_every_source(self, tmp_path: Path) -> None:
        """The rule ``--tag`` already uses, so one list keeps working."""
        _write(tmp_path, "yaml_draft.md", "---\ntags: [draft]\n---\nbody\n")
        _write(tmp_path, "clean.md", "nothing\n")
        spec = DefaultFilters(exclude_tags=["draft"])
        assert _names(tmp_path, defaults=spec) == {"clean.md"}
