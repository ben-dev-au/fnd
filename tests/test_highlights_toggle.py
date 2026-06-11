"""Highlight overlay toggle (`h`).

Pressing ``h`` toggles the search-term highlight overlay in the
preview pane on / off without re-running the query. The current
query stays cached, so flipping it back on restores the same
highlights without another search round-trip.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.render import HIGHLIGHT_STYLE
from fnd.tui import FNDApp
from fnd.tui.widgets.markdown import FNDMarkdown
from tests._pilot_wait import safe_pause, safe_press, wait_until


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def md_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    _write(
        a / "Notes.md",
        "# Patterns\n\nThe templates pattern is described here.\n",
    )
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _has_highlight_span(pane: VerticalScroll) -> bool:
    """True when at least one block under any FNDMarkdown carries a
    highlight span (yellow or orange)."""
    for md in pane.query(FNDMarkdown):
        for block in md.query("MarkdownBlock"):
            content = getattr(block, "_content", None)
            if content is None:
                continue
            for span in content.spans:
                if str(span.style) == HIGHLIGHT_STYLE:
                    return True
    return False


@pytest.mark.asyncio
async def test_highlights_default_on(cfg: Config, md_index: Path) -> None:
    """Spawning the app with a query yields highlighted spans without
    any user interaction — the default state is "highlights on"."""
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes", initial_query="templates")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: _has_highlight_span(pane),
            timeout=15.0,
            message="expected highlights on by default",
        )
        assert app._search.highlights_enabled is True


@pytest.mark.asyncio
async def test_h_key_toggles_highlights_off_then_on(cfg: Config, md_index: Path) -> None:
    """Pressing ``h`` once removes every highlight span; pressing it
    again restores them — same query, no re-search."""
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes", initial_query="templates")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        pane = app.query_one("#preview_pane", VerticalScroll)
        await wait_until(
            pilot,
            lambda: _has_highlight_span(pane),
            timeout=15.0,
            message="initial highlights never rendered",
        )

        # Toggle off.
        await safe_press(pilot, "h")
        await wait_until(
            pilot,
            lambda: app._search.highlights_enabled is False and not _has_highlight_span(pane),
            timeout=15.0,
            message="highlights didn't clear after first toggle",
        )

        # Toggle back on — same query, highlights restored.
        await safe_press(pilot, "h")
        await wait_until(
            pilot,
            lambda: app._search.highlights_enabled is True and _has_highlight_span(pane),
            timeout=15.0,
            message="highlights didn't restore after second toggle",
        )


@pytest.mark.asyncio
async def test_h_typed_in_query_bar_does_not_toggle(cfg: Config, md_index: Path) -> None:
    """The Input widget absorbs typed characters before app-level
    bindings fire, so typing ``h`` into the query bar must NOT toggle
    the overlay (otherwise users couldn't search for words containing
    'h')."""
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        # Default focus is the query input on launch with no query.
        await safe_press(pilot, "h")
        await safe_pause(pilot)
        assert app._search.highlights_enabled is True
