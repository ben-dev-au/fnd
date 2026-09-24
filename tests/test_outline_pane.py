"""The Outline pane, driven through the real app: what it lists, where its
cursor sits as the reader moves, and where Enter sends the preview."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Tree

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.outline_model import NO_OUTLINE
from fnd.tui.preview.match_row import heading_rows
from fnd.tui.preview.tuning import MATCH_CONTEXT_FRACTION
from fnd.tui.widgets.outline_tree import OutlineTree
from tests._pilot_wait import safe_pause, safe_press, wait_until

TERM = "quartzfin"


def _filler(label: str, n: int = 14) -> str:
    return "".join(f"{label} filler paragraph {i} with nothing to find.\n\n" for i in range(n))


GUIDE = (
    "# Guide\n\n"
    + f"The guide opens with {TERM}.\n\n"
    + _filler("intro")
    + "## Install\n\n"
    + _filler("install")
    + "### Homebrew\n\n"
    + f"Brew also mentions {TERM}.\n\n"
    + _filler("brew")
    + "### Pip\n\n"
    + _filler("pip")
    + "## Usage\n\n"
    + _filler("usage")
    + "## Troubleshooting\n\n"
    + f"Fixes for {TERM} problems.\n\n"
    + _filler("trouble")
)


def _notebook(findings: str = _filler("findings"), intro: str = f"Notebook {TERM} intro.") -> str:
    cell = (
        "# Analysis\n\n"
        + f"{intro}\n\n"
        + _filler("first")
        + "## Setup\n\n"
        + _filler("setup")
        + "## Findings\n\n"
        + findings
    )
    nb = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": cell},
            {
                "cell_type": "code",
                "metadata": {},
                "outputs": [],
                "source": "# not a heading\nx = 1",
            },
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(nb)


@pytest.fixture
def corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(GUIDE, encoding="utf-8")
    (docs / "tool.py").write_text(f"def run():\n    return '{TERM}'\n", encoding="utf-8")
    (docs / "analysis.ipynb").write_text(_notebook(), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


# ── helpers ──────────────────────────────────────────────────────────────


def _outline(app: FNDApp) -> OutlineTree:
    return app.query_one("#outline_pane", OutlineTree)


def _titles(app: FNDApp) -> list[str]:
    return [e.title for e in _outline(app).outline.entries]


def _visible(app: FNDApp) -> list[str]:
    tree = _outline(app)
    return [str(line.node.label) for line in tree._tree_lines]


def _cursor_title(app: FNDApp) -> str | None:
    tree = _outline(app)
    index = tree.cursor_index()
    return None if index is None else tree.outline.entries[index].title


def _entry(app: FNDApp, title: str) -> tuple[int, Any]:
    entries = _outline(app).outline.entries
    index = next(i for i, e in enumerate(entries) if e.title == title)
    return index, entries[index]


def _landed(app: FNDApp) -> bool:
    pane = app.query_one("#preview_pane")
    return not (
        app._preview_scroll.is_settling
        or app._preview.pipeline_busy()
        or app.animator.is_being_animated(pane, "scroll_y")
    )


def _followed(app: FNDApp) -> bool:
    """The outline has seen the current navigation and sampled its landing."""
    view = app._outline
    return view._anchor is app._preview_scroll.anchor and view._sampled is not None


async def _results_ready(pilot: Any, app: FNDApp) -> None:
    await wait_until(pilot, lambda: bool(app._search.groups), message="no results")


async def _open(pilot: Any, app: FNDApp, name: str, hit: int = 0, *, outlined: bool = True) -> None:
    """Put the results cursor on ``name``'s ``hit``-th section and wait for the
    preview (and unless told otherwise, the outline) to show that file."""
    tree = app.query_one("#results_pane", Tree)
    tree.focus()
    file_node = next(
        n
        for n in tree.root.children
        if Path(n.data["group"].path).name == name  # type: ignore[index]
    )
    file_node.expand()
    await safe_pause(pilot)
    _ = tree._tree_lines  # builds the lines, so .line is current
    tree.move_cursor(file_node.children[hit])
    parent_id = file_node.data["group"].parent_id  # type: ignore[index]

    def showing() -> str | None:
        return app._outline._parent_id if outlined else app._preview.showing_parent()

    await wait_until(
        pilot,
        lambda: showing() == parent_id and _landed(app),
        timeout=20,
        message=f"never showed {name}",
    )
    if outlined and _outline(app).outline.entries:
        # The follow has sampled this landing, so it cannot move the cursor later.
        await wait_until(pilot, lambda: _followed(app), message="landing never sampled")


async def _focus_outline(pilot: Any, app: FNDApp) -> OutlineTree:
    tree = _outline(app)
    tree.focus()
    await wait_until(pilot, lambda: tree.has_focus, message="the outline never took focus")
    return tree


def _hit_seq(app: FNDApp, name: str, needle: str) -> int:
    group = next(g for g in app._search.groups if Path(g.path).name == name)
    chunks = app._preview.chunk_cache.get(group.parent_id) or []
    return next(c.chunk_seq for c in chunks if needle in c.body_md or needle in c.body_text)


def _reading_line(app: FNDApp) -> int:
    pane = app.query_one("#preview_pane")
    return pane.scrollable_content_region.y + int(pane.size.height * MATCH_CONTEXT_FRACTION)


async def _jump(pilot: Any, app: FNDApp, title: str, key: str = "enter") -> Any:
    """Jump with ``key``: Enter keeps the outline focused, Right on a leaf
    moves focus into the preview (so the follow runs during the landing)."""
    index, entry = _entry(app, title)
    tree = await _focus_outline(pilot, app)
    tree.place_cursor(index)
    await safe_pause(pilot)
    await safe_press(pilot, key)
    await wait_until(
        pilot,
        lambda: (
            app._preview_scroll.anchor is not None
            and app._preview_scroll.anchor.intent == "chunk_top"
            and app._preview_scroll.anchor.focus_chunk_seq == entry.chunk_seq
            and _landed(app)
        ),
        timeout=20,
        message=f"jump to {title!r} never landed",
    )
    return entry


# ── what it lists ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_outline_lists_the_previewed_documents_headings(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        assert _titles(app) == ["Guide", "Install", "Homebrew", "Pip", "Usage", "Troubleshooting"]
        # Second level showing, third folded under its collapsed parent.
        assert _visible(app) == ["Guide", "Install", "Usage", "Troubleshooting"]
        assert _outline(app).border_title == "Outline: 6 headings"


@pytest.mark.asyncio
async def test_a_code_file_says_it_has_no_outline(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "tool.py")
        assert _titles(app) == []
        assert _visible(app) == [NO_OUTLINE]
        assert _outline(app).border_title == "Outline"


@pytest.mark.asyncio
async def test_the_pane_keeps_its_height_across_files(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        results = app.query_one("#results_pane")
        before = (_outline(app).region.height, results.region.height)
        await _open(pilot, app, "tool.py")
        await safe_pause(pilot)
        assert (_outline(app).region.height, results.region.height) == before
        await _open(pilot, app, "analysis.ipynb")
        await safe_pause(pilot)
        assert (_outline(app).region.height, results.region.height) == before


# ── where its cursor sits ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_cursor_follows_results_navigation(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        group = next(g for g in app._search.groups if Path(g.path).name == "guide.md")
        wanted = {_hit_seq(app, "guide.md", "Fixes for"): "Troubleshooting"}
        # Homebrew sits under the collapsed Install, so the cursor shows Install.
        wanted[_hit_seq(app, "guide.md", "Brew also")] = "Install"
        for hit_index, hit in enumerate(group.hits):
            if hit.chunk_seq not in wanted:
                continue
            await _open(pilot, app, "guide.md", hit=hit_index)
            await wait_until(
                pilot,
                lambda seq=hit.chunk_seq: _cursor_title(app) == wanted[seq],
                message=f"cursor stayed on {_cursor_title(app)!r}",
            )


@pytest.mark.asyncio
async def test_a_manual_scroll_moves_the_cursor(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        # Let the follow record the landing before the reader moves.
        await wait_until(pilot, lambda: app._outline._sampled is not None, message="no baseline")
        _, usage = _entry(app, "Usage")
        pane = app.query_one("#preview_pane")
        widget = app._preview.chunk_widgets[usage.chunk_seq]
        rows = heading_rows(widget) or (0,)
        target = widget.virtual_region.y + rows[0] - int(pane.size.height * MATCH_CONTEXT_FRACTION)
        pane.scroll_to(y=target + 2, animate=False)
        await wait_until(
            pilot,
            lambda: _cursor_title(app) == "Usage",
            message=f"cursor stayed on {_cursor_title(app)!r}",
        )


async def _scroll_reading_line_to(pilot: Any, app: FNDApp, title: str) -> None:
    """Scroll the preview (as a reader would) until ``title``'s heading sits
    just above the reading line."""
    _, entry = _entry(app, title)
    pane = app.query_one("#preview_pane")
    widget = app._preview.chunk_widgets[entry.chunk_seq]
    rows = heading_rows(widget) or (0,)
    fraction = int(pane.size.height * MATCH_CONTEXT_FRACTION)
    pane.scroll_to(y=widget.virtual_region.y + rows[0] - fraction + 2, animate=False)
    await safe_pause(pilot)


async def _ticks(pilot: Any, app: FNDApp, n: int = 3) -> None:
    """Let the outline's poll run ``n`` more times."""
    seen: list[int] = []
    original = app._outline.tick

    def counted() -> None:
        seen.append(1)
        original()

    app._outline.tick = counted  # type: ignore[method-assign]
    try:
        await wait_until(pilot, lambda: len(seen) >= n, message="the outline poll stopped")
    finally:
        app._outline.tick = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_browsing_the_outline_is_never_overridden(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        await _scroll_reading_line_to(pilot, app, "Usage")
        await wait_until(pilot, lambda: _cursor_title(app) == "Usage", message="no baseline")
        tree = await _focus_outline(pilot, app)
        tree.place_cursor(_entry(app, "Troubleshooting")[0])
        # A scroll that would move the cursor to Guide if the follow were live.
        app.query_one("#preview_pane").scroll_to(y=0, animate=False)
        await _ticks(pilot, app)
        assert _cursor_title(app) == "Troubleshooting"


@pytest.mark.asyncio
async def test_a_jump_places_the_cursor_on_its_target_only(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        placed: list[str | None] = []
        original = app._outline._place

        def spy(tree: Any, index: int | None) -> None:
            placed.append(None if index is None else tree.outline.entries[index].title)
            original(tree, index)

        app._outline._place = spy  # type: ignore[method-assign]
        await _jump(pilot, app, "Troubleshooting", key="right")
        assert app.focused is app.query_one("#preview_pane")
        await wait_until(pilot, lambda: _followed(app), message="the landing was never sampled")
        await _ticks(pilot, app)
        assert set(placed) == {"Troubleshooting"}, placed


# ── where Enter sends the preview ────────────────────────────────────────


@pytest.mark.asyncio
async def test_enter_lands_the_heading_on_the_reading_line(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        entry = await _jump(pilot, app, "Usage")
        widget = app._preview.chunk_widgets[entry.chunk_seq]
        rows = heading_rows(widget)
        assert rows
        # Screen regions catch up with a scroll on the next render.
        await wait_until(
            pilot,
            lambda: widget.region.y + rows[0] == _reading_line(app),
            message="the heading never painted on the reading line",
        )
        assert app._preview_scroll.reading_position(MATCH_CONTEXT_FRACTION)[0] == entry.chunk_seq  # type: ignore[index]
        assert _cursor_title(app) == "Usage"
        assert not app._current_match_unlocatable()


@pytest.mark.asyncio
async def test_reselecting_a_result_after_a_jump_lands_on_its_match(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        group = next(g for g in app._search.groups if Path(g.path).name == "guide.md")
        seq = _hit_seq(app, "guide.md", "Fixes for")
        hit_index = next(i for i, h in enumerate(group.hits) if h.chunk_seq == seq)
        await _jump(pilot, app, "Troubleshooting")
        await _open(pilot, app, "guide.md", hit=hit_index)
        await wait_until(
            pilot,
            lambda: (
                app._preview_scroll.anchor is not None
                and app._preview_scroll.anchor.intent == "first_match"
                and app._preview_scroll.anchor.focus_chunk_seq == seq
                and _landed(app)
            ),
            message="the results selection kept the heading landing",
        )


@pytest.mark.asyncio
async def test_a_heading_later_in_a_chunk_lands_individually(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "analysis.ipynb")
        assert _titles(app) == ["Analysis", "Setup", "Findings"]
        entry = await _jump(pilot, app, "Findings")
        assert entry.ordinal == 2
        widget = app._preview.chunk_widgets[entry.chunk_seq]
        await wait_until(pilot, lambda: bool(heading_rows(widget)), message="no heading rows")
        rows = heading_rows(widget)
        assert rows is not None
        await wait_until(
            pilot,
            lambda: widget.region.y + rows[2] == _reading_line(app),
            message="the heading never painted on the reading line",
        )
        await wait_until(pilot, lambda: _cursor_title(app) == "Findings", message="cursor moved")


@pytest.mark.asyncio
async def test_a_jump_is_refused_while_the_results_cursor_is_on_another_file(
    corpus: Path,
) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        before = app._preview_scroll.anchor
        results = app.query_one("#results_pane", Tree)
        results.focus()
        for _ in range(12):  # an Option-skim: the cursor moves, the preview stays
            await safe_press(pilot, "alt+down")
            target = app._preview.cursor_target()
            if target is not None and target[0] != app._outline._parent_id:
                break
        assert app._preview.cursor_target()[0] != app._outline._parent_id  # type: ignore[index]
        assert app._outline.jump(_entry(app, "Usage")[0]) is False
        assert app._preview_scroll.anchor is before


def _short_tail_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    docs = tmp_path / "tail"
    docs.mkdir()
    body = f"# Report\n\n{TERM} opens it.\n\n" + _filler("body", 30) + "## Coda\n\nThe end.\n"
    (docs / "report.md").write_text(body, encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_heading_the_scroll_cannot_reach_keeps_the_cursor(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=_short_tail_corpus(tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "report.md")
        entry = await _jump(pilot, app, "Coda", key="right")
        assert app.focused is app.query_one("#preview_pane")
        position = app._preview_scroll.reading_position(MATCH_CONTEXT_FRACTION)
        assert position is not None
        assert position[0] != entry.chunk_seq, "the fixture's tail reaches the reading line"
        await wait_until(pilot, lambda: _followed(app), message="the landing was never sampled")
        await _ticks(pilot, app)
        assert _cursor_title(app) == "Coda"


def _short_notebook(tmp_path: Path, tmp_index_dir: Path) -> Path:
    docs = tmp_path / "nb"
    docs.mkdir()
    (docs / "short.ipynb").write_text(_notebook("The end.\n"), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_heading_inside_a_chunk_the_scroll_cannot_reach_keeps_the_cursor(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=_short_notebook(tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "short.ipynb")
        entry = await _jump(pilot, app, "Findings", key="right")
        position = app._preview_scroll.reading_position(MATCH_CONTEXT_FRACTION)
        assert position is not None
        assert position[0] == entry.chunk_seq
        passed = app._outline._headings_passed(_outline(app).outline, *position)
        assert passed == 2, "the fixture's tail brings Findings to the reading line"
        await wait_until(pilot, lambda: _followed(app), message="the landing was never sampled")
        await _ticks(pilot, app)
        assert _cursor_title(app) == "Findings"


def _end_notebook(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """The only match sits under Findings, too near the end to reach the line."""
    docs = tmp_path / "nb"
    docs.mkdir()
    tail = _notebook(f"The {TERM} finding.\n\nThe end.\n", intro="Notebook intro.")
    (docs / "end.ipynb").write_text(tail, encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_match_the_scroll_cannot_reach_puts_the_cursor_on_its_heading(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=_end_notebook(tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "end.ipynb")
        position = app._preview_scroll.reading_position(MATCH_CONTEXT_FRACTION)
        assert position is not None
        passed = app._outline._headings_passed(_outline(app).outline, *position)
        assert passed == 2, "the fixture's tail brings the match to the reading line"
        await _ticks(pilot, app)
        assert _cursor_title(app) == "Findings"


@pytest.mark.asyncio
async def test_a_reading_view_round_trip_keeps_the_matchs_heading(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=_end_notebook(tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "end.ipynb")
        await _ticks(pilot, app)
        assert _cursor_title(app) == "Findings"
        for _ in range(2):  # on, then off: the restore need not land on the same row
            done = app._preview_scroll.restores_completed
            app.action_toggle_reading_mode()
            await wait_until(
                pilot,
                lambda done=done: app._preview_scroll.restores_completed > done,
                message="the view was never restored",
            )
        await wait_until(pilot, lambda: _outline(app).region.height > 0, message="never back")
        await _ticks(pilot, app)
        assert _cursor_title(app) == "Findings"


@pytest.mark.asyncio
async def test_a_scroll_before_the_outline_first_samples_the_landing_is_followed(
    corpus: Path,
) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        # The outline cannot sample while the mount's tail keeps the pipeline busy.
        landed = app._outline._landed
        app._outline._landed = lambda: False  # type: ignore[method-assign]
        await _open(pilot, app, "analysis.ipynb", outlined=False)
        pane = app.query_one("#preview_pane")
        widget = next(w for w in app._preview.chunk_widgets.values() if heading_rows(w))
        rows = heading_rows(widget)
        assert rows is not None
        fraction = int(pane.size.height * MATCH_CONTEXT_FRACTION)
        pane.scroll_to(y=widget.virtual_region.y + rows[2] - fraction + 2, animate=False)
        await safe_pause(pilot)
        app._outline._landed = landed  # type: ignore[method-assign]
        await wait_until(pilot, lambda: bool(_titles(app)), message="no outline")
        await _ticks(pilot, app)
        assert _cursor_title(app) == "Findings", "the landing outlived the scroll"


# ── keys ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_o_focuses_the_outline_and_escape_returns_to_results(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        await safe_press(pilot, "o")
        assert app.focused is _outline(app)
        assert "-focused" in _outline(app).classes
        assert app._focus_context() == "outline"
        await safe_press(pilot, "escape")
        assert app.focused is app.query_one("#results_pane")


@pytest.mark.asyncio
async def test_right_expands_a_folded_heading_then_enters_the_preview(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        tree = await _focus_outline(pilot, app)
        tree.place_cursor(_entry(app, "Install")[0])
        await safe_press(pilot, "right")
        assert _cursor_title(app) == "Homebrew", "expanding should drop onto the first child"
        await safe_press(pilot, "down")
        assert _cursor_title(app) == "Pip"
        _, pip = _entry(app, "Pip")
        await safe_press(pilot, "right")
        await wait_until(
            pilot,
            lambda: (
                app.focused is app.query_one("#preview_pane")
                and app._preview_scroll.anchor is not None
                and app._preview_scroll.anchor.focus_chunk_seq == pip.chunk_seq
                and app._preview_scroll.anchor.intent == "chunk_top"
            ),
            message="Right on a leaf did not enter the preview at that heading",
        )


@pytest.mark.asyncio
async def test_right_on_the_readers_heading_only_moves_focus(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        await wait_until(pilot, lambda: _cursor_title(app) is not None, message="no cursor")
        before = app._preview_scroll.anchor
        await safe_press(pilot, "o")
        await safe_press(pilot, "right")
        assert app.focused is app.query_one("#preview_pane")
        assert app._preview_scroll.anchor is before, "it navigated although already there"


@pytest.mark.asyncio
async def test_right_under_a_folded_heading_the_reader_is_in_does_not_jump(
    corpus: Path,
) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        group = next(g for g in app._search.groups if Path(g.path).name == "guide.md")
        seq = _hit_seq(app, "guide.md", "Brew also")
        await _open(
            pilot,
            app,
            "guide.md",
            hit=next(i for i, h in enumerate(group.hits) if h.chunk_seq == seq),
        )
        homebrew = _entry(app, "Homebrew")[0]
        await wait_until(pilot, lambda: app._outline._reader == homebrew, message="reader")
        assert _cursor_title(app) == "Install", "the folded parent stands in for Homebrew"
        before = app._preview_scroll.anchor
        await safe_press(pilot, "o")
        await safe_press(pilot, "right")  # unfold Install onto Homebrew, where the reader is
        assert _cursor_title(app) == "Homebrew"
        await safe_press(pilot, "right")
        assert app.focused is app.query_one("#preview_pane")
        assert app._preview_scroll.anchor is before, "it jumped away from the reader's place"


@pytest.mark.asyncio
async def test_left_collapses_the_pane_and_right_reopens_it(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        tree = await _focus_outline(pilot, app)
        tree.place_cursor(_entry(app, "Guide")[0])
        await safe_press(pilot, "left")  # folds Guide
        await safe_press(pilot, "left")  # at the top: the pane itself
        assert "collapsed" in tree.classes
        assert "outline_pane" in app._scope.collapsed_panels
        assert str(tree.border_title).startswith("▶ ")
        await safe_press(pilot, "right")
        assert "collapsed" not in tree.classes
        assert "outline_pane" not in app._scope.collapsed_panels


@pytest.mark.asyncio
async def test_reading_view_hides_the_outline(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        titles = _titles(app)
        app.action_toggle_reading_mode()
        await wait_until(pilot, lambda: _outline(app).region.height == 0, message="still shown")
        app.action_toggle_reading_mode()
        await wait_until(pilot, lambda: _outline(app).region.height > 0, message="never back")
        assert _titles(app) == titles


# ── a flat (untextured) PDF with bookmarks ───────────────────────────────


@pytest.fixture
def bookmarked_pdf(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import pymupdf  # type: ignore[import-not-found]

    monkeypatch.setenv("_FND_FORCE_FLAT", "1")
    papers = tmp_path / "papers"
    papers.mkdir()
    doc = pymupdf.open()
    try:
        for page_idx in range(8):
            page = doc.new_page(width=612, height=792)
            y = 60.0
            for line_idx in range(34):
                text = f"page {page_idx} line {line_idx} ordinary prose"
                if page_idx in (0, 4) and line_idx == 20:
                    text = f"page {page_idx} holds the {TERM} term"
                page.insert_text((72, y), text, fontsize=11, fontname="helv")
                y += 20
        doc.set_toc(
            [
                [1, "Part One", 1],
                [2, "Opening", 1],
                [2, "Middle", 3],
                [1, "Part Two", 5],
                [2, "Closing", 7],
            ]
        )
        doc.save(str(papers / "book.pdf"))
    finally:
        doc.close()
    build_index(roots=[papers], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_bookmarked_pdf_outlines_its_bookmarks_and_jumps(bookmarked_pdf: Path) -> None:
    app = FNDApp(index_dir=bookmarked_pdf, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "book.pdf")
        assert app._flat.active_buffer is not None, "expected the flat preview"
        assert _titles(app) == ["Part One", "Opening", "Middle", "Part Two", "Closing"]
        entry = await _jump(pilot, app, "Closing")
        position = app._preview_scroll.reading_position(MATCH_CONTEXT_FRACTION)
        assert position is not None
        assert position[0] == entry.chunk_seq
        await wait_until(pilot, lambda: _cursor_title(app) == "Closing", message="cursor")


@pytest.mark.asyncio
async def test_a_flat_jump_then_its_result_returns_to_the_match(bookmarked_pdf: Path) -> None:
    app = FNDApp(index_dir=bookmarked_pdf, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "book.pdf")
        group = app._search.groups[0]
        await _jump(pilot, app, "Part Two")
        _, part_two = _entry(app, "Part Two")
        hit_index = next(i for i, h in enumerate(group.hits) if h.chunk_seq == part_two.chunk_seq)
        await _open(pilot, app, "book.pdf", hit=hit_index)
        await wait_until(
            pilot,
            lambda: (
                app._preview_scroll.anchor is not None
                and app._preview_scroll.anchor.intent == "first_match"
                and app._preview_scroll.anchor.focus_chunk_seq == part_two.chunk_seq
            ),
            message="the stale latch swallowed the load back to the match",
        )


# ── a far, unmounted section ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_jump_to_a_far_unmounted_section_lands(tmp_path: Path, tmp_index_dir: Path) -> None:
    from tests._preview_corpus import wide_doc

    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "wide.md")
        _, far = _entry(app, "Section 290")
        assert far.chunk_seq not in app._preview.chunk_widgets, "fixture no longer windowed"
        entry = await _jump(pilot, app, "Section 290")
        position = app._preview_scroll.reading_position(MATCH_CONTEXT_FRACTION)
        assert position is not None
        assert position[0] == entry.chunk_seq


@pytest.mark.asyncio
async def test_an_outline_resize_does_not_ask_to_relabel_the_results() -> None:
    from textual.app import App, ComposeResult

    from fnd.tui.widgets.results_tree import ResultsTree

    class _Harness(App[None]):
        seen = 0

        def compose(self) -> ComposeResult:
            yield OutlineTree("Outline")

        def on_results_tree_geometry_changed(self, _event: ResultsTree.GeometryChanged) -> None:
            self.seen += 1

    app = _Harness()
    async with app.run_test(size=(60, 20)) as pilot:
        await safe_pause(pilot)
        await pilot.resize_terminal(60, 30)
        await safe_pause(pilot)
        await pilot.resize_terminal(80, 12)
        await safe_pause(pilot)
        assert app.seen == 0


@pytest.mark.asyncio
async def test_the_reading_position_is_current_straight_after_a_scroll(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        _, usage = _entry(app, "Usage")
        pane = app.query_one("#preview_pane")
        widget = app._preview.chunk_widgets[usage.chunk_seq]
        fraction = int(pane.size.height * MATCH_CONTEXT_FRACTION)
        pane.scroll_to(y=widget.virtual_region.y - fraction + 3, animate=False, immediate=True)
        # No refresh in between: on-screen regions still show the old position.
        position = app._preview_scroll.reading_position(MATCH_CONTEXT_FRACTION)
        assert position is not None
        assert position[0] == usage.chunk_seq


# ── Left from the preview goes back where Right came from ────────────────


async def _right_from_outline(pilot: Any, app: FNDApp, title: str) -> None:
    tree = await _focus_outline(pilot, app)
    tree.place_cursor(_entry(app, title)[0])
    await safe_press(pilot, "right")
    await wait_until(
        pilot, lambda: app.focused is app.query_one("#preview_pane"), message="not in preview"
    )


@pytest.mark.asyncio
async def test_left_returns_to_the_outline_it_came_from(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        for _ in range(2):  # and again: the route is remembered each time
            await _right_from_outline(pilot, app, "Usage")
            await safe_press(pilot, "left")
            assert app.focused is _outline(app)


@pytest.mark.asyncio
async def test_left_returns_to_results_after_any_other_way_in(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        results = app.query_one("#results_pane")
        preview = app.query_one("#preview_pane")

        await _right_from_outline(pilot, app, "Usage")
        await safe_press(pilot, "p")  # already there: the route stands
        await safe_press(pilot, "left")
        assert app.focused is _outline(app)

        await _right_from_outline(pilot, app, "Usage")
        await safe_press(pilot, "r")
        await safe_press(pilot, "p")  # a shortcut in, not the bridge
        await safe_press(pilot, "left")
        assert app.focused is results

        await _right_from_outline(pilot, app, "Usage")
        await safe_press(pilot, "o")
        await pilot.click("#preview_pane")  # a click in
        await wait_until(pilot, lambda: app.focused is preview, message="click did not focus")
        await safe_press(pilot, "left")
        assert app.focused is results


@pytest.mark.asyncio
async def test_left_still_returns_to_the_outline_after_the_terminal_refocuses(
    corpus: Path,
) -> None:
    from textual import events

    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        preview = app.query_one("#preview_pane")
        await _right_from_outline(pilot, app, "Usage")
        app.post_message(events.AppBlur())
        await wait_until(pilot, lambda: app.focused is None, message="blur kept focus")
        app.post_message(events.AppFocus())
        await wait_until(pilot, lambda: app.focused is preview, message="focus not restored")
        await safe_press(pilot, "left")
        assert app.focused is _outline(app)


# ── staying put while hidden or re-rendered ──────────────────────────────


@pytest.mark.asyncio
async def test_a_highlight_rerender_keeps_the_outline_and_its_folds(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        tree = _outline(app)
        tree._heading_nodes[_entry(app, "Install")[0]].expand()
        await safe_pause(pilot)
        assert "Homebrew" in _visible(app)
        shown: list[str] = []
        original_show = app._outline._show

        def spy(t: OutlineTree, placeholder: str) -> None:
            shown.append(placeholder)
            original_show(t, placeholder)

        app._outline._show = spy  # type: ignore[method-assign]
        searcher = app._search.searcher
        assert searcher is not None
        slow_decode = searcher.get_file_chunks

        def slow(*args: Any, **kwargs: Any) -> Any:
            time.sleep(0.6)  # past a few outline polls
            return slow_decode(*args, **kwargs)

        monkeypatch.setattr(searcher, "get_file_chunks", slow)
        old_chunks = app._outline._chunks
        await safe_press(pilot, "h")
        await wait_until(
            pilot,
            lambda: app._outline._chunks is not None and app._outline._chunks is not old_chunks,
            timeout=20,
            message="the re-render never reached the outline",
        )
        assert shown == [], "the outline blanked during the re-render"
        assert "Homebrew" in _visible(app), "the re-render refolded the outline"


@pytest.mark.asyncio
async def test_a_collapsed_outline_catches_up_when_reopened(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        tree = await _focus_outline(pilot, app)
        tree.place_cursor(_entry(app, "Guide")[0])
        await safe_press(pilot, "left")
        await safe_press(pilot, "left")
        assert "collapsed" in tree.classes
        builds: list[object] = []
        original_build = app._outline._build

        def counted(parent_id: str, chunks: Any) -> Any:
            builds.append(parent_id)
            return original_build(parent_id, chunks)

        app._outline._build = counted  # type: ignore[method-assign]
        await _open(pilot, app, "analysis.ipynb", outlined=False)
        await _ticks(pilot, app)
        assert builds == [], "built an outline nobody can see"
        assert _titles(app) == []
        assert str(tree.border_title) == "▶ Outline"
        await _focus_outline(pilot, app)
        await safe_press(pilot, "right")
        await wait_until(
            pilot,
            lambda: _titles(app) == ["Analysis", "Setup", "Findings"],
            message="reopening never outlined the previewed file",
        )


@pytest.mark.asyncio
async def test_browsing_the_outline_still_tracks_where_the_reader_is(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        await _scroll_reading_line_to(pilot, app, "Usage")
        await wait_until(pilot, lambda: _cursor_title(app) == "Usage", message="no baseline")
        await _focus_outline(pilot, app)
        trouble, _ = _entry(app, "Troubleshooting")
        await _scroll_reading_line_to(pilot, app, "Troubleshooting")  # the wheel, while browsing
        await wait_until(pilot, lambda: app._outline._reader == trouble, message="reader lost")
        assert _cursor_title(app) == "Usage"
        _, usage = _entry(app, "Usage")
        await safe_press(pilot, "right")  # back to the heading the cursor is on
        await wait_until(
            pilot,
            lambda: (
                app._preview_scroll.anchor is not None
                and app._preview_scroll.anchor.intent == "chunk_top"
                and app._preview_scroll.anchor.focus_chunk_seq == usage.chunk_seq
            ),
            message="Right trusted a reader the outline had stopped tracking",
        )


async def _jump_then_scroll_to_the_top(pilot: Any, app: FNDApp, title: str, top: str) -> Any:
    """Enter on ``title`` (the outline keeps focus), then the wheel to the very top."""
    await _jump(pilot, app, title)
    await wait_until(pilot, lambda: _followed(app), message="the landing was never sampled")
    app.query_one("#preview_pane").scroll_to(y=0, animate=False)
    first = _entry(app, top)[0]
    await wait_until(pilot, lambda: app._outline._reader == first, message="reader lost")
    assert _cursor_title(app) == title, "moved the cursor the user is browsing"
    return app._preview_scroll.anchor


# A heading the scroll brings to the reading line, and one too near the end to.
_JUMPS = [("guide.md", "Usage", "Guide"), ("report.md", "Coda", "Report")]


def _jump_corpus(name: str, corpus: Path, tmp_path: Path, tmp_index_dir: Path) -> Path:
    return corpus if name == "guide.md" else _short_tail_corpus(tmp_path, tmp_index_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "title", "top"), _JUMPS)
async def test_right_after_scrolling_to_the_top_jumps_back_to_the_heading(
    name: str, title: str, top: str, corpus: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=_jump_corpus(name, corpus, tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, name)
        jumped = await _jump_then_scroll_to_the_top(pilot, app, title, top)
        _, entry = _entry(app, title)
        await safe_press(pilot, "right")
        await wait_until(
            pilot,
            lambda: (
                app._preview_scroll.anchor is not jumped
                and app._preview_scroll.anchor is not None
                and app._preview_scroll.anchor.focus_chunk_seq == entry.chunk_seq
            ),
            message="Right trusted the reader of the earlier jump",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "title", "top"), _JUMPS)
async def test_leaving_the_outline_brings_its_cursor_to_the_reader(
    name: str, title: str, top: str, corpus: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=_jump_corpus(name, corpus, tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, name)
        await _jump_then_scroll_to_the_top(pilot, app, title, top)
        await safe_press(pilot, "r")
        await wait_until(pilot, lambda: _cursor_title(app) == top, message="the cursor stayed")


@pytest.mark.asyncio
async def test_an_outline_built_after_the_reader_scrolled_shows_where_they_are(
    corpus: Path,
) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        tree = await _focus_outline(pilot, app)
        tree.place_cursor(_entry(app, "Guide")[0])
        await safe_press(pilot, "left")
        await safe_press(pilot, "left")
        assert "collapsed" in tree.classes
        await _open(pilot, app, "analysis.ipynb", outlined=False)
        # The reader scrolls into Findings while the pane is shut.
        pane = app.query_one("#preview_pane")
        widget = next(w for w in app._preview.chunk_widgets.values() if heading_rows(w))
        rows = heading_rows(widget)
        assert rows is not None
        assert len(rows) == 3
        fraction = int(pane.size.height * MATCH_CONTEXT_FRACTION)
        pane.scroll_to(y=widget.virtual_region.y + rows[2] - fraction + 2, animate=False)
        await _ticks(pilot, app)
        placed: list[str | None] = []
        original = app._outline._place

        def spy(tree: Any, index: int | None) -> None:
            placed.append(None if index is None else tree.outline.entries[index].title)
            original(tree, index)

        app._outline._place = spy  # type: ignore[method-assign]
        await _focus_outline(pilot, app)
        await safe_press(pilot, "right")  # reopen: the outline builds now
        await wait_until(pilot, lambda: bool(_titles(app)), message="never built")
        await safe_press(pilot, "r")
        await wait_until(
            pilot, lambda: _cursor_title(app) == "Findings", message="replayed the old landing"
        )
        assert "Analysis" not in placed, placed


def _long_tail_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    docs = tmp_path / "parts"
    docs.mkdir()
    parts = [f"# Top\n\nOpening {TERM} line.\n\n"]
    parts += [f"## Part {i}\n\n" + _filler(f"part {i}", 3) for i in range(12)]
    parts.append("## Last\n\n" + _filler("tail", 11))
    (docs / "parts.md").write_text("".join(parts), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_scrolling_up_from_a_heading_held_at_the_end_follows_the_reader(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=_long_tail_corpus(tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "parts.md")
        await _jump(pilot, app, "Last")  # Enter: the outline keeps focus
        await wait_until(pilot, lambda: _followed(app), message="the landing was never sampled")
        pane = app.query_one("#preview_pane")
        assert pane.scroll_offset.y == pane.max_scroll_y, "the fixture's tail reaches the line"
        jumped = app._preview_scroll.anchor
        pane.scroll_to(y=pane.max_scroll_y - 22, animate=False)
        last, _ = _entry(app, "Last")
        await wait_until(
            pilot,
            lambda: app._outline._reader is not None and app._outline._reader < last - 1,
            message="the reader stayed on the heading the reader scrolled away from",
        )
        await safe_press(pilot, "right")  # back to Last
        await wait_until(
            pilot,
            lambda: app._preview_scroll.anchor is not jumped,
            message="Right trusted a reader still pinned to Last",
        )


@pytest.mark.asyncio
async def test_a_reopened_outline_places_its_cursor_while_it_has_focus(corpus: Path) -> None:
    app = FNDApp(index_dir=corpus, initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "guide.md")
        tree = await _focus_outline(pilot, app)
        tree.place_cursor(_entry(app, "Guide")[0])
        await safe_press(pilot, "left")
        await safe_press(pilot, "left")
        await _open(pilot, app, "analysis.ipynb", outlined=False)
        await _focus_outline(pilot, app)
        await safe_press(pilot, "right")  # reopen, and so focused
        await wait_until(pilot, lambda: bool(_titles(app)), message="never built")
        assert tree.has_focus
        await wait_until(pilot, lambda: _cursor_title(app) == "Analysis", message="no cursor")


@pytest.mark.asyncio
async def test_a_resize_that_moves_the_held_heading_off_screen_follows_the_reader(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=_short_tail_corpus(tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(120, 45)) as pilot:
        await _results_ready(pilot, app)
        await _open(pilot, app, "report.md")
        coda, _ = _entry(app, "Coda")
        await _jump(pilot, app, "Coda")
        await wait_until(pilot, lambda: _followed(app), message="the landing was never sampled")
        await _ticks(pilot, app)
        assert app._outline._reader == coda, "the fixture's tail reaches the reading line"
        pane = app.query_one("#preview_pane")
        before = pane.scroll_offset.y
        await pilot.resize_terminal(120, 30)  # Coda drops off screen
        report, _ = _entry(app, "Report")
        await wait_until(
            pilot, lambda: app._outline._reader == report, message="the reader stayed on Coda"
        )
        assert pane.scroll_offset.y == before, "the offset moved, so this no longer tests a resize"
