"""The sample was scanned once at mount and never again.

So every number beside a file type described the rules the screen OPENED with.
A typed rule selecting nothing left `Markdown · 6, Plain text · 5` on a screen
whose own expression matched zero, and the save that followed removed eleven
documents. The pane was the only thing that disagreed with the config, the
walk and the indexer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from tests._pilot_wait import wait_until


@pytest.fixture
def built_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\n\nrisotto.\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return tmp_index_dir


_TAG = "tag:frontmatter:draft"
_TAGS = {"frontmatter": {"draft": 2}}


def _provider(seen: list[Any]) -> Any:
    """A stand-in for the walk: it answers from the spec it is handed, so the
    test measures whether the screen re-asks, not how the walk counts."""

    def sample(spec: Any = None) -> SourceSample:
        seen.append(spec)
        if spec is not None and getattr(spec, "expression", ""):
            return SourceSample(kinds={"md": 6}, kinds_kept={}, gated=True, tags=_TAGS)
        return SourceSample(kinds={"md": 6}, kinds_kept={"md": 6}, gated=True, tags=_TAGS)

    return sample


async def _open(app: FNDApp, pilot: Any, seen: list[Any]) -> FilterBrowserScreen:
    screen = FilterBrowserScreen(
        title="Index filters",
        spec=FilterSpec(kinds=("md",)),
        gitignore=True,
        fndignore=True,
        sample_provider=_provider(seen),
        on_save=lambda *_a: None,
    )
    app.push_screen(screen)
    await wait_until(
        pilot,
        lambda: app.screen is screen and bool(screen.query("#filter_tree")),
        timeout=20.0,
        message="the browser never composed",
    )
    await wait_until(
        pilot,
        lambda: screen._sample is not None,
        timeout=20.0,
        message="the first scan never landed",
    )
    return screen


@pytest.mark.asyncio
async def test_a_new_rule_makes_the_counts_be_recomputed(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index)
    seen: list[Any] = []
    async with app.run_test(size=(110, 34)) as pilot:
        screen = await _open(app, pilot, seen)
        first = len(seen)
        assert first >= 1, "the mount scan is the baseline"

        # What `t` does when a typed rule is applied.
        screen._spec = FilterSpec(kinds=("md",), expression="file.path == 'x'")
        screen._rebuild(focus_tree=False)
        await wait_until(
            pilot,
            lambda: len(seen) > first,
            timeout=20.0,
            message="the counts were never recomputed for the new rule",
        )
        await wait_until(
            pilot,
            lambda: screen._sampled_spec == screen._spec,
            timeout=20.0,
            message="the sample never caught up with the spec",
        )
        kept = dict(screen._sample.kinds_kept)

    assert getattr(seen[-1], "expression", "") == "file.path == 'x'", seen[-1]
    assert kept == {}, "the rule selects nothing, and the counts must say so"


@pytest.mark.asyncio
async def test_an_unchanged_spec_is_not_rescanned(built_index: Path) -> None:
    """The control. A rebuild happens on every keystroke in the row filter, and
    each scan walks the source."""
    app = FNDApp(index_dir=built_index)
    seen: list[Any] = []
    async with app.run_test(size=(110, 34)) as pilot:
        screen = await _open(app, pilot, seen)
        first = len(seen)

        for _ in range(3):
            screen._rebuild(focus_tree=False)
            await pilot.pause()
        # Real time, not ticks: the debounce is a 0.3s timer, so a scan it had
        # scheduled would have run by now.
        await asyncio.sleep(0.8)
        await pilot.pause()

    assert len(seen) == first, f"{len(seen) - first} needless scans"


@pytest.mark.asyncio
async def test_ticking_a_tag_recounts(built_index: Path) -> None:
    """The commonest gesture, and the one path that did not reach the check.

    `_on_selection` calls `_rebuild` only when the tree and the spec disagree.
    An ordinary tick round-trips exactly, so it takes the early return: the
    selection below is the real one a tag row produces, verified to round-trip
    through `apply_selection` and `selection_for`.
    """
    from fnd.filters.tree_model import selection_for
    from fnd.tui.widgets.toggle_tree import ToggleTree

    app = FNDApp(index_dir=built_index)
    seen: list[Any] = []
    async with app.run_test(size=(110, 34)) as pilot:
        screen = await _open(app, pilot, seen)
        first = len(seen)

        selected, _ = selection_for(screen._spec, gitignore=True, fndignore=True)
        tree = screen.query_one("#filter_tree", ToggleTree)
        screen._on_selection(
            ToggleTree.SelectionChanged(
                tree, selected=frozenset(selected), excluded=frozenset({_TAG})
            )
        )
        assert screen._spec.exclude_tags, "the tick has to change the spec to be a tick"

        await wait_until(
            pilot,
            lambda: len(seen) > first,
            timeout=20.0,
            message="a tick never reached the stale check",
        )
        asked = seen[-1]

    assert getattr(asked, "exclude_tags", None), asked


@pytest.mark.asyncio
async def test_a_file_type_tick_buys_no_walk(built_index: Path) -> None:
    """The control on the cost. `_sample` strips `kinds` before building the
    gate, so a file-type tick cannot change a count and must not walk."""
    app = FNDApp(index_dir=built_index)
    seen: list[Any] = []
    async with app.run_test(size=(110, 34)) as pilot:
        screen = await _open(app, pilot, seen)
        first = len(seen)

        screen._spec = FilterSpec(kinds=("md", "txt"))
        screen._rebuild(focus_tree=False)
        await asyncio.sleep(0.8)
        await pilot.pause()

    assert len(seen) == first, f"{len(seen) - first} walks for a kinds-only change"
