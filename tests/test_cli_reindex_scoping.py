"""``fnd collection reindex`` scopes through ``-c``, like ``fnd search``.

``-c all`` means every collection here exactly as it does for a search, so one
token says "everything" across the whole CLI. Naming nothing stays an error:
re-indexing every collection is a minutes-long rebuild, so it is asked for
rather than defaulted into — but the error proposes ``-c all`` through the same
report-and-offer path a typo goes through, instead of Typer's bare
"Missing argument 'NAME'".
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


def test_omitting_the_collection_proposes_all_and_indexes_nothing(
    cli: tuple[CliRunner, list[str]],
) -> None:
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex"])

    assert result.exit_code == 2
    assert "no collection given" in result.output
    assert "-c all" in result.output
    assert touched == []


def test_c_all_covers_every_collection(cli: tuple[CliRunner, list[str]]) -> None:
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "-c", "all"])

    assert result.exit_code == 0, result.output
    assert sorted(touched) == ["books", "notes"]


def test_a_single_name_still_scopes_to_it(cli: tuple[CliRunner, list[str]]) -> None:
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "-c", "notes"])

    assert result.exit_code == 0, result.output
    assert touched == ["notes"]


def test_a_comma_list_names_several(cli: tuple[CliRunner, list[str]]) -> None:
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "-c", "notes,books"])

    assert result.exit_code == 0, result.output
    assert sorted(touched) == ["books", "notes"]


def test_a_typo_fails_instead_of_widening(cli: tuple[CliRunner, list[str]]) -> None:
    """The failure mode this scoping must not introduce: a mistyped name
    quietly re-indexing the entire corpus."""
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "-c", "noets"])

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
    result = runner.invoke(app, ["collection", "reindex", "-c", "all", "--rebuild"])

    assert result.exit_code == 0, result.output
    assert seen == [True, True]


def test_a_bare_name_still_works(cli: tuple[CliRunner, list[str]]) -> None:
    """`fnd collection reindex WBT` predates -c; keep it working rather than
    breaking a form already in use."""
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "notes"])

    assert result.exit_code == 0, result.output
    assert touched == ["notes"]


def test_naming_the_collection_twice_is_refused(cli: tuple[CliRunner, list[str]]) -> None:
    runner, touched = cli
    result = runner.invoke(app, ["collection", "reindex", "notes", "-c", "books"])

    assert result.exit_code == 2
    assert touched == []
