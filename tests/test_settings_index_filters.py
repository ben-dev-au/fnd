"""The Index filters settings rows, and the per-source override screen."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.tui import FNDApp
from tests._pilot_wait import settings_ready


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from fnd.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_the_indexing_screen_has_one_filters_row(built_index: Path) -> None:
    """One drill into the browser, not a column of typed fields."""
    from fnd.tui.menu import SECTION_INDEXING_PDF_TEXTURE
    from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings_section

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING_PDF_TEXTURE)
        await settings_ready(pilot, app)
        assert isinstance(app.screen, SettingsScreen)
        items = app.screen.query_one(SettingsList)._items
        filter_rows = [it for it in items if it.id.startswith("filters.")]
        assert [it.id for it in filter_rows] == ["filters.browse"], (
            f"expected one filters row, got {[it.id for it in filter_rows]}"
        )
        assert filter_rows[0].subsection == "Index filters"


@pytest.mark.asyncio
async def test_source_form_exposes_a_filters_drill(built_index: Path) -> None:
    from fnd.tui.settings_screen import SettingsList, SourceFormScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="default", source_index=None))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        items = form.query_one(SettingsList)._items
        row = next(it for it in items if it.id == "form.filters")
        assert row.label == "Index filters"
        assert row.value_getter is not None
        assert row.value_getter(app) == "inherited"


@pytest.mark.asyncio
async def test_filters_drill_opens_override_rows(built_index: Path) -> None:
    from fnd.tui.settings_screen import SettingsList, SourceFormScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="default", source_index=None))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        lst = form.query_one(SettingsList)
        lst.cursor_index = next(i for i, it in enumerate(lst._items) if it.id == "form.filters")
        await pilot.press("enter")
        await pilot.pause()
        from fnd.tui.settings_screen import FilterBrowserScreen

        assert isinstance(app.screen, FilterBrowserScreen), (
            "the source filters row must open the visual browser"
        )


@pytest.mark.asyncio
async def test_an_override_marks_the_form_dirty_and_an_unchanged_one_does_not(
    built_index: Path,
) -> None:
    """The reindex gate, both directions.

    Omitting ``filters`` from the snapshot would force a rebuild on every
    save; sharing the dict would make the check never fire again.
    """
    from fnd.tui.settings_screen import SourceFormScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="default", source_index=None))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._snapshot = dict(form._fields)
        form._snapshot["filters"] = dict(form._fields["filters"])
        assert form._snapshot == form._fields, "an untouched form must not look dirty"
        form._fields["filters"]["respect_gitignore"] = False
        assert form._snapshot != form._fields, "an override must be seen as a change"


def test_only_an_unset_field_inherits() -> None:
    """``-`` overrides to nothing; an untouched field inherits.

    Treating an empty list as untouched made the row's own ``-`` a no-op, so
    a source could not be exempted from a global exclusion.
    """
    from fnd.tui.settings_screen import _source_filters_or_none

    assert _source_filters_or_none({}) is None
    assert _source_filters_or_none({"respect_gitignore": None}) is None

    emptied = _source_filters_or_none({"exclude_tags": []})
    assert emptied is not None, "an explicit empty override must survive"
    assert emptied.exclude_tags == []

    got = _source_filters_or_none({"respect_gitignore": False})
    assert got is not None
    assert got.respect_gitignore is False
    assert got.exclude_tags is None, "an untouched field must stay unset so it inherits"


def test_an_emptied_override_beats_the_global_default() -> None:
    """End to end: the source keeps files the defaults would have excluded."""
    from fnd.config import DefaultFilters, SourceFilters, resolve_filters
    from fnd.tui.settings_screen import _source_filters_or_none

    defaults = DefaultFilters(exclude_tags=["no_index"], expression="file.size < 10")
    emptied = _source_filters_or_none({"exclude_tags": [], "expression": ""})
    assert isinstance(emptied, SourceFilters)

    resolved = resolve_filters(emptied, defaults)
    assert resolved.exclude_tags == []
    assert resolved.expression is None


@pytest.mark.asyncio
async def test_editing_as_text_fills_the_rows_back_in(built_index: Path) -> None:
    """The text view and the rows are two views of one set.

    Typing a row-shaped clause must populate that row on save — that is the
    "text informs the UI" half, not a mis-parse.
    """
    from textual.widgets import TextArea

    from fnd.tui.settings_screen import FilterTextScreen, _spec_from_filters

    saved: list[object] = []
    from fnd.config import SourceFilters

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            FilterTextScreen(
                title="t",
                spec=_spec_from_filters(SourceFilters()),
                on_save=saved.append,
            )
        )
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FilterTextScreen)
        screen.query_one(
            "#filter_text", TextArea
        ).text = "(file.kind in ['pdf']) AND (file.size <= 500)"
        await pilot.pause()
        screen.action_save_close()
        await pilot.pause()

    assert len(saved) == 1
    spec = saved[0]
    assert spec.kinds == ("pdf",)  # type: ignore[attr-defined]
    assert spec.max_size == 500  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_malformed_text_refuses_to_save(built_index: Path) -> None:
    from textual.widgets import TextArea

    from fnd.config import SourceFilters
    from fnd.tui.settings_screen import FilterTextScreen, _spec_from_filters

    saved: list[object] = []
    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            FilterTextScreen(
                title="t", spec=_spec_from_filters(SourceFilters()), on_save=saved.append
            )
        )
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FilterTextScreen)
        screen.query_one("#filter_text", TextArea).text = "file.kind in ["
        await pilot.pause()
        screen.action_save_close()
        await pilot.pause()
        assert saved == [], "invalid text must not be saved"
        assert isinstance(app.screen, FilterTextScreen), "screen stays open on error"


def test_a_source_override_records_only_what_differs() -> None:
    """The per-source browser shows the resolved set and stores the delta.

    Editing the effective filter and keeping only what differs from the
    defaults removes the need for an inherit state or a ``-`` sentinel.
    """
    from fnd.config import DefaultFilters, SourceFilters, resolve_filters
    from fnd.tui.settings_screen import _spec_from_filters, _spec_to_mapping

    defaults = DefaultFilters(exclude_tags=["no_index"], kinds=["md"])
    resolved = resolve_filters(SourceFilters(), defaults)
    spec = _spec_from_filters(resolved)

    # Unchanged: nothing is recorded, so the source keeps inheriting.
    from fnd.tui.settings_screen import _same_setting

    values = _spec_to_mapping(spec)
    delta = {k: v for k, v in values.items() if not _same_setting(v, getattr(defaults, k, None))}
    assert delta == {}, f"an untouched source must record no override, got {delta}"

    # Changed: only the changed field is recorded.
    from dataclasses import replace

    widened = replace(spec, exclude_tags=())
    values = _spec_to_mapping(widened)
    delta = {k: v for k, v in values.items() if not _same_setting(v, getattr(defaults, k, None))}
    assert delta == {"exclude_tags": []}, delta


@pytest.mark.asyncio
async def test_the_source_scan_does_not_block_the_screen(built_index: Path) -> None:
    """The picker scan opens files; on the event loop one cloud-evicted note
    freezes the screen for as long as the provider takes to deliver."""
    import threading

    from fnd.filters import FilterSpec
    from fnd.filters.scan import SourceSample
    from fnd.tui.settings_screen import FilterBrowserScreen
    from fnd.tui.widgets.toggle_tree import ToggleTree

    release = threading.Event()

    def provider() -> SourceSample:
        release.wait(timeout=10)
        return SourceSample(kinds={"md": 42}, tags={"os": {"slowtag": 7}})

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="t",
                spec=FilterSpec(),
                gitignore=True,
                fndignore=True,
                sample_provider=provider,
                on_save=lambda *_a: None,
            )
        )
        for _ in range(10):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FilterBrowserScreen)
        assert screen._scanning, "test setup — the scan should still be running"
        tree = screen.query_one(ToggleTree)
        assert tree.root.children, "the tree must be usable before the scan lands"
        assert "scanning" in str(screen.query_one("#filter_summary").render())

        release.set()
        for _ in range(400):
            await pilot.pause()
            if not screen._scanning:
                break
        assert not screen._scanning, "the sample never arrived"
        tags = next(n for n in tree.root.children if "Tags" in str(n.label))
        assert any("slowtag" in str(c.label) for c in tags.children)
        assert "scanning" not in str(screen.query_one("#filter_summary").render())


@pytest.mark.asyncio
async def test_the_two_filter_rows_are_not_both_called_filter(built_index: Path) -> None:
    """'Filter' sat directly above 'Index filters'; the drill looked like a
    no-op because Enter on the scalar above it opens a text field."""
    from fnd.tui.settings_screen import SettingsList, SourceFormScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="default", source_index=None))
        await pilot.pause()
        labels = {it.id: it.label for it in app.screen.query_one(SettingsList)._items}
        assert labels["form.filter"] == "Frontmatter rule"
        assert labels["form.filters"] == "Index filters"
