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
async def test_index_filter_rows_render_in_the_indexing_screen(built_index: Path) -> None:
    from fnd.tui.menu import SECTION_INDEXING_PDF_TEXTURE
    from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings_section

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING_PDF_TEXTURE)
        await settings_ready(pilot, app)
        assert isinstance(app.screen, SettingsScreen)
        items = app.screen.query_one(SettingsList)._items
        ids = {it.id for it in items}
        for required in (
            "filters.respect_gitignore",
            "filters.respect_fndignore",
            "filters.exclude_tags",
            "filters.kinds",
            "filters.min_size",
            "filters.max_size",
            "filters.created_after",
            "filters.created_before",
            "filters.modified_after",
            "filters.modified_before",
            "filters.expression",
            "filters.frontmatter",
        ):
            assert required in ids, f"missing row {required!r}"
        grouped = {it.subsection for it in items if it.id.startswith("filters.")}
        assert grouped == {"Index filters"}


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
    from fnd.tui.settings_screen import SettingsList, SettingsScreen, SourceFormScreen

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
        assert isinstance(app.screen, SettingsScreen)
        ids = {it.id for it in app.screen.query_one(SettingsList)._items}
        for required in (
            "srcfilter.respect_gitignore",
            "srcfilter.exclude_tags",
            "srcfilter.min_size",
            "srcfilter.max_size",
            "srcfilter.created_after",
            "srcfilter.expression",
        ):
            assert required in ids, f"missing per-source row {required!r}"


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


def test_optional_scalars_clear_to_unset() -> None:
    """An emptied optional row must remove the key, not write "".

    ``EditBar`` used to skip ``coerce`` on empty input, so the row posted a
    literal empty string and validation rejected every write.
    """
    from fnd.tui.menu import _coerce_optional_date, _coerce_optional_int, _coerce_str_list

    assert _coerce_optional_int("") is None
    assert _coerce_optional_date("") is None
    assert _coerce_str_list("") == []
    assert _coerce_optional_int("50_000_000") == 50_000_000


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


def test_a_dash_on_a_text_row_overrides_to_nothing() -> None:
    """Through the real row setter, not a hand-built dict.

    The previous test constructed the overrides mapping directly, so it passed
    while the row's own ``-`` was being discarded.
    """
    from fnd.config import DefaultFilters
    from fnd.tui.settings_screen import _source_filter_items

    overrides: dict[str, object] = {}
    rows = _source_filter_items(overrides, DefaultFilters(), lambda: None)

    for row_id in ("srcfilter.expression", "srcfilter.exclude_tags"):
        row = next(r for r in rows if r.id == row_id)
        assert row.coerce is not None
        assert row.scalar_setter is not None
        row.scalar_setter(None, row.coerce("-"))  # type: ignore[arg-type]

    assert overrides == {"expression": "", "exclude_tags": []}, (
        "'-' must record an explicit empty override, not clear the field"
    )

    for row_id in ("srcfilter.expression", "srcfilter.exclude_tags"):
        row = next(r for r in rows if r.id == row_id)
        row.scalar_setter(None, row.coerce(""))  # type: ignore[arg-type,misc]
    assert overrides == {}, "an emptied box must clear the override so it inherits"
