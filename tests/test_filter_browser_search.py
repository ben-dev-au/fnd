"""A long filter branch can be searched, as every other settings list can.

`/` binds to a filter on the settings list, and the filter browser (which
renders a vault's whole tag vocabulary, thousands of rows on a real corpus)
had no equivalent. Not slow; unnavigable by arrow key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from fnd.tui.widgets.toggle_tree import ToggleTree


def _sample(_spec: object = None) -> SourceSample:
    return SourceSample(
        kinds={"md": 40, "pdf": 3},
        tags={"frontmatter": {"alpha": 2, "beta": 3, "zeta": 1}, "os": {"alpine": 1}},
    )


async def _open(app: FNDApp, pilot: object) -> FilterBrowserScreen:
    app.push_screen(
        FilterBrowserScreen(
            title="Index filters",
            spec=FilterSpec(),
            gitignore=True,
            fndignore=True,
            sample_provider=_sample,
            on_save=lambda *_a: None,
        )
    )
    for _ in range(20):
        await pilot.pause()  # type: ignore[attr-defined]
    screen = app.screen
    assert isinstance(screen, FilterBrowserScreen)
    return screen


def _painted(app: FNDApp) -> str:
    return "\n".join(
        "".join(seg.text for seg in strip) for strip in app.screen._compositor.render_strips()
    )


def _labels(tree: ToggleTree) -> set[str]:
    return {it.label for g in tree._groups for it in g.leaves}


@pytest.mark.asyncio
async def test_a_query_narrows_the_rows(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        tree = screen.query_one("#filter_tree", ToggleTree)
        before = _labels(tree)
        assert any("alpha" in v for v in before)
        assert any("beta" in v for v in before)

        screen.query_one("#filter_search", Input).value = "alp"
        for _ in range(6):
            await pilot.pause()
        after = _labels(tree)

    assert any("alpha" in v for v in after)
    assert any("alpine" in v for v in after), "both sources, not just the first"
    assert not any("beta" in v for v in after), after


@pytest.mark.asyncio
async def test_a_branch_name_keeps_its_rows(tmp_index_dir: Path) -> None:
    """Searching for a branch shows the branch, not an empty one."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        screen.query_one("#filter_search", Input).value = "file types"
        for _ in range(6):
            await pilot.pause()
        labels = _labels(screen.query_one("#filter_tree", ToggleTree))

    assert any("Markdown" in v for v in labels), labels


@pytest.mark.asyncio
async def test_escape_clears_the_query_before_it_leaves(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        screen.query_one("#filter_search", Input).value = "alp"
        for _ in range(6):
            await pilot.pause()
        screen.action_back()
        await pilot.pause()

        assert app.screen is screen, "the first Esc clears, it does not leave"
        assert screen.query_one("#filter_search", Input).value == ""
        assert any("beta" in v for v in _labels(screen.query_one("#filter_tree", ToggleTree)))

        screen.action_back()
        await pilot.pause()
        assert app.screen is not screen


@pytest.mark.asyncio
async def test_the_summary_says_the_rows_are_narrowed(tmp_index_dir: Path) -> None:
    """A tree showing three of forty rows must not read as the whole set."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        screen.query_one("#filter_search", Input).value = "alp"
        for _ in range(6):
            await pilot.pause()
        # The summary is a custom renderable wrapped in a RichVisual, so
        # str() gives the wrapper. Read what was painted.
        painted = "\n".join(
            "".join(seg.text for seg in strip) for strip in app.screen._compositor.render_strips()
        )

    assert "'alp'" in painted, painted[-500:]


@pytest.mark.asyncio
async def test_the_footer_drops_the_row_keys_while_typing(tmp_index_dir: Path) -> None:
    """Adding a search box broke the class-wide guard that says a screen with
    a text box must not advertise keys that type into it. `t`, `c` and `y` are
    single letters; in the box they insert rather than act."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        # _HintBar is a renderable behind a RichVisual, so read the paint.
        idle = _painted(app)
        assert "As text" in idle, idle[-300:]

        screen.query_one("#filter_search", Input).focus()
        for _ in range(4):
            await pilot.pause()
        typing = _painted(app)

    assert "As text" not in typing, typing
    assert "Copy" not in typing, typing


@pytest.mark.asyncio
async def test_typing_reaches_the_box_and_not_the_bindings(tmp_index_dir: Path) -> None:
    """Setting Input.value drives the handler without touching focus, so the
    whole defect is invisible to a test written that way: after the first
    keystroke the tree took focus and `t`, `c` and `y` ran as bindings.
    `/cle` reached `c`: clear everything, no confirmation."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("slash")
        await pilot.pause()
        for ch in "alp":
            await pilot.press(ch)
            await pilot.pause()
        box = screen.query_one("#filter_search", Input)
        value, focused = box.value, app.focused
        # Inside the context: the stack is torn down on exit.
        still_here = app.screen is screen

    assert value == "alp", f"only {value!r} reached the box"
    assert focused is box, f"focus left the box for {focused}"
    assert still_here, "a keystroke opened another screen"


@pytest.mark.asyncio
async def test_typing_c_does_not_clear_the_filter_set(tmp_index_dir: Path) -> None:
    """The worst reachable case: `c` is Clear, unmodified and unconfirmed."""
    spec = FilterSpec(kinds=("md",))
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="Index filters",
                spec=spec,
                gitignore=True,
                fndignore=True,
                sample_provider=_sample,
                on_save=lambda *_a: None,
            )
        )
        for _ in range(20):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FilterBrowserScreen)
        await pilot.press("slash")
        await pilot.pause()
        for ch in "cle":
            await pilot.press(ch)
            await pilot.pause()
        kept = screen._spec.kinds

    assert kept == ("md",), f"typing into the box cleared the set: {kept}"


@pytest.mark.asyncio
async def test_a_landing_scan_does_not_take_focus_off_the_box(tmp_index_dir: Path) -> None:
    """The sample arrives on a worker's schedule, mid-word."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("slash")
        await pilot.pause()
        box = screen.query_one("#filter_search", Input)
        screen._sample_arrived(_sample())
        for _ in range(4):
            await pilot.pause()
        focused = app.focused

    assert focused is box, f"the scan landing moved focus to {focused}"
