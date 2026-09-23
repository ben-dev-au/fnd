"""Replacing the filter text silently revoked `no_index`.

The exclusion is rendered into the box, so deleting it reads as typing one
rule; `walk_sources` went 13 → 14 and a note tagged `no_index` entered scope,
under the message "Filters saved." The `c` Clear route was made to refuse this
act; the text route is the escape hatch, so it says what it is doing instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static, TextArea

from fnd.config import DefaultFilters, SourceFilters, resolve_filters
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterTextScreen, _protection_dropped, _spec_from_filters


def _guarded_spec() -> Any:
    return _spec_from_filters(
        resolve_filters(SourceFilters(), DefaultFilters(exclude_tags=["no_index"]))
    )


def test_the_guard_is_in_the_text_the_user_edits() -> None:
    """The premise: it is visible, which is why deleting it is so easy."""
    from fnd.filters.text_form import render

    assert "no_index" in render(_guarded_spec())


def test_dropping_it_is_detected() -> None:
    from fnd.filters.text_form import parse

    before = _guarded_spec()
    after = parse("NOT (file.kind in ['python'])")

    assert _protection_dropped(before, after) == "no_index"


def test_keeping_it_is_not_flagged() -> None:
    """The control: an edit that keeps the guard must say nothing."""
    from fnd.filters.text_form import parse, render

    before = _guarded_spec()
    after = parse(f"{render(before)} AND NOT (file.kind in ['python'])")

    assert _protection_dropped(before, after) == ""


def test_an_unrelated_tag_is_not_flagged() -> None:
    """Only the guard tag: a user dropping `draft` has seen what it does."""
    from fnd.filters.text_form import parse

    before = parse("NOT ('draft' in file.tags.all)")
    after = parse("NOT (file.kind in ['python'])")

    assert _protection_dropped(before, after) == ""


@pytest.mark.asyncio
async def test_the_screen_warns_while_it_is_being_typed(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(
            FilterTextScreen(
                title="Index filters (text)", spec=_guarded_spec(), on_save=lambda _s: None
            )
        )
        for _ in range(15):
            await pilot.pause()
        app.screen.query_one("#filter_text", TextArea).text = "NOT (file.kind in ['python'])"
        for _ in range(10):
            await pilot.pause()
        status = app.screen.query_one("#filter_status", Static)
        painted = " ".join(
            " ".join(
                "".join(s.text for s in strip).strip().strip("│").strip()
                for strip in app.screen._compositor.render_strips()
            ).split()
        )

    assert "-bad" in status.classes, "it reads as an ordinary valid rule"
    assert "drops the no_index exclusion" in painted, painted
