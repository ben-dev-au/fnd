"""A legacy config's globs select, once migrated, exactly the files they did.

Before config versioning, globs were ``fnmatch``: ``*``, ``?`` and a class that
admits ``/`` all crossed a separator, and a ``**/`` prefix also matched at the
root. ``_legacy_matches`` restates that matcher from the release that wrote
these configs, and the migrated globs are compared against it.
"""

from __future__ import annotations

import fnmatch
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from fnd import config as conf
from fnd.config_migrations import CONFIG_VERSION, migrate
from fnd.globs import GlobSet
from fnd.walk import walk

PATHS = (
    "a.md",
    "b.txt",
    "x.md",
    "ab.md",
    "README.md",
    "drafts.md",
    "2024-01-02.md",
    "a/b.md",
    "a/bb.md",
    "a/.b.md",
    "a/b/c.md",
    "a/b/c/d.md",
    "a1/b.md",
    "ax/yb.md",
    "sub/README.md",
    "notes/a.md",
    "notes/.x.md",
    "notes/sub/x.md",
    "notes/sub/deeper/y.txt",
    "notes/.trash/x.md",
    "drafts/a.md",
    "drafts/2024/a.md",
    "draftsman/n.md",
    "Projects/README.md",
    "Projects/a/README.md",
    "Projects/a/b/README.md",
    "2024-/1-02.md",
    ".x.md",
    ".hidden/a.md",
    ".obsidian/app.md",
    ".obsidian/plugins/p.md",
    "^/a.md",
    "[/a.md",
    "-/a.md",
    "!/a.md",
)

GLOBS = (
    "*.md",
    "*",
    "**",
    "**/*",
    "**/*.md",
    "**/README.md",
    "**/.x.md",
    "**/.trash/**",
    "**/b/*.md",
    "*/*.md",
    "*/b*",
    "*README.md",
    "README.md",
    "notes/*.md",
    "notes/**",
    "notes/**/*.md",
    "notes/*",
    "notes/[!.]*",
    "drafts/*",
    "drafts*",
    "draft*.md",
    "Projects/*/README.md",
    "a*b*.md",
    "a*/c.md",
    "a?b.md",
    "a?.b.md",
    "?.md",
    "a/?.md",
    "[a-c]*.md",
    "[!n]*",
    "a[!x]b.md",
    "a[/]b.md",
    "*[0-9]*.md",
    "[^a]*",
    "[]a]*",
    "[!]a]*",
    "[a-]*",
    "[z-a].md",
    "[z-ab]*",
    "[!b-a]*",
    "[--0]*",
    "[!^]*",
    "[!-]/*",
    "*.[mt][dx]*",
    ".obsidian/**",
    ".obsidian/*",
    "*/.trash/*",
    ".*",
    "**/.*",
    "build/",
    "/abs/**",
    "a//b.md",
    "",
)


def _legacy_matches(glob: str, rel: str) -> bool:
    """One glob, as the fnmatch-era walker matched it."""
    if fnmatch.fnmatchcase(rel, glob):
        return True
    return "/" not in rel and glob.startswith("**/") and fnmatch.fnmatchcase(rel, glob[3:])


def _legacy_names_hidden(globs: list[str]) -> bool:
    return any(part.startswith(".") for g in globs for part in g.split("/"))


def _legacy_selects(includes: list[str], excludes: list[str], rel: str) -> bool:
    hidden = any(part.startswith(".") for part in rel.split("/"))
    if hidden and not _legacy_names_hidden(includes):
        return False
    if includes and not any(_legacy_matches(g, rel) for g in includes):
        return False
    return not any(_legacy_matches(g, rel) for g in excludes)


def _migrated(includes: list[str], excludes: list[str]) -> tuple[list[str], list[str]]:
    source: dict[str, Any] = {"path": "~/x", "includes": list(includes), "excludes": excludes}
    migrate({"collections": {"c": {"sources": [source]}}})
    return source["includes"], source["excludes"]


def _mismatches(glob: str) -> list[str]:
    _includes, excludes = _migrated([], [glob])
    globs = GlobSet.parse(excludes)
    return [rel for rel in PATHS if globs.matches(rel) != _legacy_matches(glob, rel)]


def test_each_glob_matches_the_paths_it_did() -> None:
    """A migrated glob matches a path exactly when the fnmatch-era glob did."""
    wrong = {g: _mismatches(g) for g in GLOBS}
    assert not {g: m for g, m in wrong.items() if m}


def test_every_short_glob_matches_the_paths_it_did() -> None:
    """Exhaustive over short globs and paths, so no token pairing is missed."""
    tokens = ("a", "*", "?", "/", ".", "[!a]", "[a/]")
    globs = {"".join(p) for n in range(1, 4) for p in product(tokens, repeat=n)}
    chars = ("a", "b", ".", "/")
    paths = [
        rel
        for n in range(1, 5)
        for rel in ("".join(p) for p in product(chars, repeat=n))
        if all(seg not in ("", ".", "..") for seg in rel.split("/"))
    ]
    wrong: dict[str, list[str]] = {}
    for glob in sorted(globs):
        _includes, excludes = _migrated([], [glob])
        matched = GlobSet.parse(excludes)
        bad = [rel for rel in paths if matched.matches(rel) != _legacy_matches(glob, rel)]
        if bad:
            wrong[glob] = bad[:3]
    assert not wrong


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    for rel in PATHS:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n", encoding="utf-8")
    return root.resolve()


def _cases() -> list[tuple[list[str], list[str]]]:
    """Include lists that agree on naming a hidden component: a dotted glob opens
    the hidden prune only for its own paths, by design."""
    includes: list[list[str]] = [[], *([g] for g in GLOBS)]
    includes += [["*.md", "notes/*"], [".obsidian/**", "**/.trash/**"], ["a?.b.md", "*.txt"]]
    excludes: list[list[str]] = [[], ["notes/*"], ["drafts/*"], ["*/.trash/*"], ["a?b.md"]]
    return [(i, e) for i in includes for e in excludes]


def test_a_source_indexes_the_files_it_did(tree: Path) -> None:
    """The walk over a migrated source yields exactly what the fnmatch-era walk did."""
    wrong: dict[str, tuple[set[str], set[str]]] = {}
    for includes, excludes in _cases():
        inc, exc = _migrated(includes, excludes)
        got = {
            p.relative_to(tree).as_posix()
            for p in walk(roots=[tree], includes=inc, excludes=exc, skip_dirs=frozenset())
        }
        want = {rel for rel in PATHS if _legacy_selects(includes, excludes, rel)}
        if got != want:
            wrong[f"{includes} - {excludes}"] = (got - want, want - got)
    assert not wrong


def test_a_nested_file_stays_in_the_index_end_to_end(tree: Path, tmp_path: Path) -> None:
    """Through the file, the loader and the source walk, as a user upgrading meets it."""
    from fnd.walk import walk_sources

    path = tmp_path / "config.toml"
    path.write_text(
        f'[[collections.n.sources]]\npath = "{tree}"\n'
        'includes = ["notes/*.md", "drafts/*", "Projects/*/README.md"]\n'
        'excludes = ["notes/sub/deeper/*"]\n',
        encoding="utf-8",
    )
    conf.ensure_current(path)
    source = conf.load(path).collections["n"].sources[0]
    got = {p.relative_to(tree).as_posix() for p in walk_sources(sources=[source])}
    assert {"notes/sub/x.md", "drafts/2024/a.md", "Projects/a/b/README.md"} <= got
    assert "notes/sub/deeper/y.txt" not in got


def test_the_flat_legacy_shape_is_migrated_too() -> None:
    """Collection-level globs become the promoted source's, so they need the same rewrite."""
    raw: dict[str, Any] = {
        "collections": {"c": {"roots": ["~/x"], "includes": ["*.md"], "excludes": ["drafts/*"]}}
    }
    migrate(raw)
    source = conf.Config.model_validate(raw).collections["c"].sources[0]
    assert source.includes == ["**/*.md"]
    assert source.excludes == ["drafts/**"]


def test_a_glob_too_long_to_spell_out_is_narrowed_and_named() -> None:
    """Past the cap a glob only loses paths where a ``?`` stood for ``/``, and says so."""
    source: dict[str, Any] = {"path": "~/x", "includes": ["????-??-??.md", "notes/*.md"]}
    _version, applied = migrate({"collections": {"c": {"sources": [source]}}})
    assert source["includes"] == ["????-??-??.md", "notes/**/*.md"]
    narrowed = GlobSet.parse(source["includes"][:1])
    assert {r for r in PATHS if narrowed.matches(r)} == {"2024-01-02.md"}
    assert _legacy_matches("????-??-??.md", "2024-/1-02.md"), "the premise: a reading is lost"
    assert [note for note in applied if "'????-??-??.md'" in note], applied
    assert not [note for note in applied if "notes/" in note], "an exact glob was reported"


def test_a_current_config_keeps_its_globs() -> None:
    """Only a legacy glob is fnmatch; one written by this version already means what it says."""
    source = {"path": "~/x", "includes": ["notes/*.md", "*.md"], "excludes": ["a?b"]}
    migrate({"config_version": CONFIG_VERSION, "collections": {"c": {"sources": [source]}}})
    assert source == {"path": "~/x", "includes": ["notes/*.md", "*.md"], "excludes": ["a?b"]}
