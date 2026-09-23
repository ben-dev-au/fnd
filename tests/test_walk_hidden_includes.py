"""Only a glob naming a dot-prefixed component admits a hidden path.

``.obsidian/**`` added beside ``**/*.md`` must not lift the hidden prune for the
whole tree: an Obsidian vault would index every note the user had deleted into
``.trash``, which is not in DEFAULT_JUNK_DIRS (that list holds macOS's
``.Trashes``). Private material entering a searchable index, with nothing on
any screen saying the prune had been lifted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.walk import walk


def _corpus(root: Path, *rels: str) -> None:
    for rel in rels:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")


def _walk(root: Path, includes: list[str] | None) -> set[str]:
    return {str(p.relative_to(root)) for p in walk(roots=[root], includes=includes)}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    _corpus(
        tmp_path,
        ".obsidian/plugins/readme.md",
        ".trash/deleted-secrets.md",
        "Notes/keep.md",
    )
    return tmp_path


def test_no_includes_hides_the_hidden(vault: Path) -> None:
    assert _walk(vault, None) == {"Notes/keep.md"}


def test_a_dotted_glob_admits_only_what_it_matches(vault: Path) -> None:
    got = _walk(vault, ["**/*.md", ".obsidian/**"])
    assert got == {"Notes/keep.md", ".obsidian/plugins/readme.md"}


def test_a_dotted_glob_alone_still_reaches_its_folder(vault: Path) -> None:
    assert _walk(vault, [".obsidian/**"]) == {".obsidian/plugins/readme.md"}


def test_an_undotted_glob_matching_a_hidden_path_is_not_consent(vault: Path) -> None:
    """``**/*.md`` matches ``.trash/deleted-secrets.md``. That is not enough."""
    assert ".trash/deleted-secrets.md" not in _walk(vault, ["**/*.md"])


def test_one_dotted_glob_does_not_open_the_other_hidden_paths(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        ".config/wanted.md",
        ".env.md",
        ".secret/leak.md",
        "visible/.private.md",
        "visible/v.md",
    )
    assert _walk(tmp_path, [".config/**", "**/*.md"]) == {".config/wanted.md", "visible/v.md"}
