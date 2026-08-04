"""``fnd collection reindex`` scopes the way ``fnd search`` does.

Naming nothing used to be an error ("Missing argument 'NAME'"), which made
re-indexing everything a shell loop the user had to write, and made the two
halves of the CLI disagree about what an unspecified collection means. Now an
omitted name — and the ``all`` pseudo-name — covers every configured
collection, while a typo still has to fail rather than silently widen.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import app

CONFIG = """
    [[collections.notes.sources]]
    path = "{a}"

    [[collections.books.sources]]
    path = "{b}"
"""


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[CliRunner, list[str]]:
    """A runner over a two-collection config, plus the list of collections each
    reindex actually touched (the index build itself is stubbed — this is about
    scope resolution, not indexing)."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(CONFIG).format(
            a=(tmp_path / "a").as_posix(), b=(tmp_path / "b").as_posix()
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    touched: list[str] = []

    def fake_build(*, collection: str, **_kwargs: object) -> int:
        touched.append(collection)
        return 7

    monkeypatch.setattr("fnd.index.build_index_from_config", fake_build)
    return CliRunner(), touched


def test_omitted_name_covers_every_collection(cli: tuple[CliRunner, list[str]]) -> None:
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex"])

    assert result.exit_code == 0, result.output
    assert sorted(touched) == ["books", "notes"]


def test_all_pseudo_name_covers_every_collection(cli: tuple[CliRunner, list[str]]) -> None:
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "all"])

    assert result.exit_code == 0, result.output
    assert sorted(touched) == ["books", "notes"]


def test_a_single_name_still_scopes_to_it(cli: tuple[CliRunner, list[str]]) -> None:
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "notes"])

    assert result.exit_code == 0, result.output
    assert touched == ["notes"]


def test_a_comma_list_names_several(cli: tuple[CliRunner, list[str]]) -> None:
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "notes,books"])

    assert result.exit_code == 0, result.output
    assert sorted(touched) == ["books", "notes"]


def test_a_typo_fails_instead_of_widening(cli: tuple[CliRunner, list[str]]) -> None:
    """The failure mode this scoping must not introduce: a mistyped name
    quietly re-indexing the entire corpus."""
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "noets"])

    assert result.exit_code != 0
    assert touched == []


def test_rebuild_flag_reaches_every_target(
    monkeypatch: pytest.MonkeyPatch, cli: tuple[CliRunner, list[str]]
) -> None:
    runner, _touched = cli
    seen: list[bool] = []

    def fake_build(*, collection: str, rebuild: bool, **_kwargs: object) -> int:
        seen.append(rebuild)
        return 0

    monkeypatch.setattr("fnd.index.build_index_from_config", fake_build)
    result = runner.invoke(app, ["collection", "reindex", "--rebuild"])

    assert result.exit_code == 0, result.output
    assert seen == [True, True]
