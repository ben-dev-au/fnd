"""A query that matched nothing says so, and names what narrowed it.

Zero results painted two blank panes and the words "Type a query and press
Enter", over the query the user had just pressed Enter on. A filter set in an
earlier session survives in `scope.toml` and narrows every search after it,
which is what makes the blank screen unreadable rather than merely bare.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import run_search


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.papers.sources]]
            path = "{(tmp_path / "papers").as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def indexed(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "papers"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text("saffron risotto\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="papers")
    return tmp_index_dir


def _painted(app: FNDApp) -> str:
    return "\n".join(
        "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
    )


@pytest.mark.asyncio
async def test_it_names_the_query_instead_of_asking_for_one(cfg: Config, indexed: Path) -> None:
    app = FNDApp(index_dir=indexed, config=cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await run_search(pilot, app, "zzzznotfound")
        for _ in range(10):
            await pilot.pause()
        on_screen = _painted(app)
        title = app.query_one("#results_pane", Tree).border_title

    assert "No results for 'zzzznotfound'" in on_screen, on_screen[:400]
    assert "Type a query and press Enter" not in on_screen, "it asked for the query it was given"
    assert "nothing matched" in str(title), title


@pytest.mark.asyncio
async def test_a_filter_that_emptied_the_search_is_named(cfg: Config, indexed: Path) -> None:
    """The trap this exists for: a kind filter from an earlier session leaves
    every search empty, and nothing on screen connects the two."""
    app = FNDApp(index_dir=indexed, config=cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app._scope.filter_kinds.append("python")
        await run_search(pilot, app, "risotto")
        for _ in range(10):
            await pilot.pause()
        on_screen = _painted(app)

    assert "No results for 'risotto'" in on_screen, on_screen[:400]
    assert "1 filter is narrowing this" in on_screen, on_screen[:400]
    assert "Filters panel" in on_screen, on_screen[:400]


@pytest.mark.asyncio
async def test_it_names_a_place_and_not_a_key(cfg: Config, indexed: Path) -> None:
    """Focus is in the query bar when this paints, and a key pressed there
    types itself into the query, which is correct. So the message said
    `X clears it` and produced `risottoX`, with the filter untouched. It names
    the panel, which carries a row that works whatever has focus.
    """
    app = FNDApp(index_dir=indexed, config=cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app._scope.filter_kinds.append("python")
        await run_search(pilot, app, "risotto")
        for _ in range(10):
            await pilot.pause()
        message = app._results.empty_state()
        key = app._fnd_keymap.for_action("clear_filters")

    assert key, "the premise: the action has a key"
    assert key not in message, f"it advertised {key!r}, which types into the query bar"
    assert "Filters panel" in message, message


@pytest.mark.asyncio
async def test_a_search_that_finds_something_says_none_of_it(cfg: Config, indexed: Path) -> None:
    """The control: the empty state must not survive the next query."""
    app = FNDApp(index_dir=indexed, config=cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await run_search(pilot, app, "zzzznotfound")
        for _ in range(10):
            await pilot.pause()
        await run_search(pilot, app, "saffron")
        for _ in range(20):
            await pilot.pause()
        on_screen = _painted(app)
        title = app.query_one("#results_pane", Tree).border_title

    assert "No results" not in on_screen, on_screen[:400]
    assert "nothing matched" not in str(title), title


@pytest.mark.asyncio
async def test_an_untouched_app_still_asks_for_a_query(cfg: Config, indexed: Path) -> None:
    """The other control: before any search, the pane's instruction is right."""
    app = FNDApp(index_dir=indexed, config=cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        for _ in range(10):
            await pilot.pause()
        on_screen = _painted(app)
        title: Any = app.query_one("#results_pane", Tree).border_title

    assert "Type a query and press Enter" in on_screen
    assert str(title) == "Results", title
