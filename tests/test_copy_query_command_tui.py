"""End-to-end for the copy-query-as-command feature.

Covers both halves: the ``tui`` launch flags hydrate the scope (relaunch),
and the ``copy_query_command`` action serialises live scope back out (copy).
The two are inverses — ``test_seed_then_snapshot_round_trips`` asserts it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Input

from fnd.config import Config, load
from fnd.index import build_index
from fnd.launch_command import LaunchCommandSerializer, LaunchScope, _flatten
from fnd.tui import FNDApp


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent("""
            [defaults]
            tag_sources = ["frontmatter"]

            [[collections.wine.sources]]
            path = "/tmp/wine"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: p)
    return load(p)


@pytest.fixture
def wine_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "wine"
    _write(root / "Cabernet.md", "# Cabernet\n\nCabernet aging notes.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="wine")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_launch_filters_seed_scope(cfg: Config, wine_index: Path) -> None:
    """`fnd tui`'s filter flags populate the live scope on startup."""
    launch = LaunchScope(
        created="week",
        modified="month",
        kinds=("pdf",),
        tags=("red",),
        not_tags=("draft",),
        tag_match_all=False,
    )
    app = FNDApp(index_dir=wine_index, config=cfg, collection="wine", launch_filters=launch)
    async with app.run_test() as pilot:
        await pilot.pause()
        s = app._scope
        assert s.collections == ["wine"]
        assert s.filter_created == "week"
        assert s.filter_date == "month"
        assert s.filter_kinds == ["pdf"]
        assert s.tag_include == {"frontmatter": {"red"}}
        assert s.tag_exclude == {"frontmatter": {"draft"}}
        assert s.tag_match_all is False


@pytest.mark.asyncio
async def test_seed_then_snapshot_round_trips(cfg: Config, wine_index: Path) -> None:
    """Hydrate ← serialize are inverses: seed a scope from a LaunchScope,
    snapshot it, and recover the same LaunchScope."""
    launch = LaunchScope(
        created="week",
        modified="month",
        kinds=("pdf", "md"),
        tags=("red", "white"),
        not_tags=("draft",),
        tag_match_all=False,
    )
    app = FNDApp(index_dir=wine_index, config=cfg, collection="wine", launch_filters=launch)
    async with app.run_test() as pilot:
        await pilot.pause()
        snap = app._scope.snapshot("q")
        rebuilt = LaunchScope(
            created=None if snap.filter_created == "any" else snap.filter_created,
            modified=None if snap.filter_date == "any" else snap.filter_date,
            kinds=tuple(snap.filter_kinds),
            tags=tuple(sorted(_flatten(snap.tag_include))),
            not_tags=tuple(sorted(_flatten(snap.tag_exclude))),
            tag_match_all=snap.tag_match_all,
        )
        assert rebuilt == launch


@pytest.mark.asyncio
async def test_action_copies_expected_command(
    cfg: Config, wine_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str] = []
    monkeypatch.setattr("fnd.tui.clipboard.copy_text", lambda text: captured.append(text))
    app = FNDApp(
        index_dir=wine_index,
        config=cfg,
        collection="wine",
        initial_query="cabernet aging",
        launch_filters=LaunchScope(created="week", kinds=("pdf",)),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._search.current_query == "cabernet aging"
        app.action_copy_query_command()
        await pilot.pause()
        expected = "fnd 'cabernet aging' -c wine --created week --kind pdf"
        assert captured == [expected]
        # The action and the serializer agree on the live snapshot.
        assert (
            expected
            == LaunchCommandSerializer(app._scope.snapshot("cabernet aging")).serialize().command
        )


@pytest.mark.asyncio
async def test_ctrl_y_fires_from_query_bar(
    cfg: Config, wine_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str] = []
    monkeypatch.setattr("fnd.tui.clipboard.copy_text", lambda text: captured.append(text))
    app = FNDApp(index_dir=wine_index, config=cfg, collection="wine", initial_query="cabernet")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#query_bar", Input).focus()
        await pilot.pause()
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert captured == ["fnd cabernet -c wine"]


@pytest.mark.asyncio
async def test_nothing_to_copy_skips_clipboard(
    cfg: Config, wine_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("fnd.tui.clipboard.copy_text", lambda text: calls.append(text))
    app = FNDApp(index_dir=wine_index, config=cfg)  # no scope, no query, no filters
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_copy_query_command()
        await pilot.pause()
        assert calls == []


@pytest.mark.asyncio
async def test_tag_fanned_across_sources_counts_once(cfg: Config, wine_index: Path) -> None:
    """A source-agnostic `--tag` seeds into every source, but the search treats
    it as one OR-ed term — the active-filter count must not double it. Reopening
    `fnd Tree -c DPC --tag strategy-pattern` used to report 2 tags active."""
    app = FNDApp(index_dir=wine_index, config=cfg, collection="wine")
    async with app.run_test() as pilot:
        await pilot.pause()
        s = app._scope
        s.tag_include = {"frontmatter": {"strategy-pattern"}, "os": {"strategy-pattern"}}
        assert s.active_filter_count == 1
        assert s._distinct_tag_values(s.tag_include) == {"strategy-pattern"}
        # A genuinely different value still counts separately.
        s.tag_exclude = {"os": {"draft"}}
        assert s.active_filter_count == 2
