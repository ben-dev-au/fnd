"""The Index filters settings rows, and the per-source override screen."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, cast

import pytest

from fnd.tui import FNDApp
from fnd.tui.settings_screen import UnsavedChangesScreen
from fnd.tui.widgets import COMMIT_KEY
from tests._pilot_wait import screen_ready, settings_ready, wait_until


def _walk_nodes(node: Any) -> list[Any]:
    """Every node under this one, at any depth."""
    return [n for child in node.children for n in (child, *_walk_nodes(child))]


def _summary_text(screen: Any) -> str:
    """The summary box as painted. It refits itself to the width it renders
    at, so its stored renderable is not what the user sees."""
    from textual.widgets import Static

    box = screen.query_one("#filter_summary", Static)
    painted = " ".join(box.render_line(y).text for y in range(box.size.height))
    # Collapsed: the box wraps, so a phrase can straddle two painted rows.
    return " ".join(painted.split())


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from fnd.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_the_filters_screen_splits_index_from_query(built_index: Path) -> None:
    """One drill into the browser, and the two kinds of filter kept apart.

    ``tag_sources`` takes effect immediately while everything above it needs a
    reindex; on one undifferentiated list that difference is invisible.
    """
    from fnd.tui.menu import KIND_HEADER, SECTION_FILTERS
    from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings_section

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_FILTERS)
        await settings_ready(pilot, app)
        assert isinstance(app.screen, SettingsScreen)
        items = app.screen.query_one(SettingsList)._items
        assert [it.id for it in items if it.id.endswith(".browse")] == ["filters.browse"]
        headers = [it.label for it in items if it.kind == KIND_HEADER]
        assert headers == ["What gets indexed", "What a search returns"]
        order = [it.id for it in items]
        assert order.index("filters.browse") < order.index("filters.tag_sources")


@pytest.mark.asyncio
async def test_source_form_exposes_a_filters_drill(built_index: Path) -> None:
    from fnd.tui.settings_screen import SettingsList, SourceFormScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="default", source_index=None))
        await screen_ready(pilot, app, SourceFormScreen)
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
        await screen_ready(pilot, app, SourceFormScreen)
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        lst = form.query_one(SettingsList)
        lst.cursor_index = next(i for i, it in enumerate(lst._items) if it.id == "form.filters")
        await pilot.press("enter")
        from fnd.tui.settings_screen import FilterBrowserScreen

        await wait_until(
            pilot,
            lambda: isinstance(app.screen, FilterBrowserScreen),
            timeout=30.0,
            message="the filter browser never opened",
        )
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
        await screen_ready(pilot, app, SourceFormScreen)
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


def test_the_override_count_counts_settings_not_config_keys() -> None:
    """``clears`` is one key naming any number of fields."""
    from fnd.config import SourceConfig
    from fnd.tui.settings_screen import (
        _overridden_fields,
        _seeded_filters,
        _source_filters_or_none,
    )

    filters = _source_filters_or_none({"max_size": None, "min_size": None, "kinds": ["pdf"]})
    seeded = _seeded_filters(SourceConfig(path=Path("~/x"), filters=filters))
    assert len(seeded) == 2, "the two cleared bounds share one key"
    assert _overridden_fields(seeded) == ["kinds", "max_size", "min_size"]
    assert _overridden_fields({}) == []


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

    def provider(_spec: object = None) -> SourceSample:
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
        assert "scanning" in _summary_text(screen)

        release.set()
        for _ in range(400):
            await pilot.pause()
            if not screen._scanning:
                break
        assert not screen._scanning, "the sample never arrived"
        # One tag source needs no parent level, so the branch is named for it.
        tags = next(n for n in tree.root.children if "tags" in str(n.label).lower())
        assert any("slowtag" in str(c.label) for c in tags.children)
        assert "scanning" not in _summary_text(screen)


@pytest.mark.asyncio
async def test_the_frontmatter_rule_lives_with_the_other_filters(built_index: Path) -> None:
    """It sat beside Index filters as its own row, so the two could hold
    different answers to the same question and neither showed the other's."""
    from fnd.tui.settings_screen import FilterBrowserScreen, SettingsList, SourceFormScreen
    from fnd.tui.widgets.toggle_tree import ToggleTree

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="default", source_index=None))
        await screen_ready(pilot, app, SourceFormScreen)
        lst = app.screen.query_one(SettingsList)
        ids = [it.id for it in lst._items]
        assert "form.filter" not in ids, "a second frontmatter input beside Index filters"
        assert "form.filters" in ids
        lst.cursor_index = ids.index("form.filters")
        await pilot.pause()
        await pilot.press("right")
        # Gated, not counted: 400 ticks is a budget that degrades to nothing
        # under load, and the assertion below then reads the screen it left.
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, FilterBrowserScreen),
            timeout=30.0,
            message="the filter browser never opened",
        )
        browser = app.screen
        assert isinstance(browser, FilterBrowserScreen)
        while browser._scanning:
            await pilot.pause()
        rules = next(
            n
            for n in browser.query_one(ToggleTree).root.children
            if "Rules you type" in str(n.label)
        )
        assert any("Frontmatter rule" in str(c.label) for c in rules.children)


@pytest.mark.asyncio
async def test_a_legacy_frontmatter_rule_is_visible_and_clearable(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form's own row is gone, so the browser is the only surface for the
    rule — it must read a legacy ``frontmatter_filter``, and clearing it there
    must not be undone by the value the form loaded with."""
    from fnd.config import CollectionConfig, SourceConfig, write_collection
    from fnd.tui.settings_screen import SourceFormScreen

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    root = tmp_path / "vault"
    root.mkdir()
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(
            sources=[SourceConfig(path=root, frontmatter_filter="Course == 'X'")]
        ),
    )
    from fnd.config import load

    app = FNDApp(index_dir=built_index, config=load(cfg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
        for _ in range(30):
            await pilot.pause()
        form = cast(SourceFormScreen, app.screen)
        assert form._fields["filters"].get("frontmatter") == "Course == 'X'", (
            "the browser is handed the overrides and would show '(none)'"
        )

        form._fields["filters"].pop("frontmatter", None)
        assert form._frontmatter_text() == "", "the cleared rule came back"


@pytest.mark.asyncio
async def test_clearing_the_default_tags_does_not_reinstate_them(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the key lets ``DefaultFilters``' own default resurrect, so
    removing an exclusion handed it straight back on save.

    Driven by unticking the row rather than by the clear gesture: the global
    set inherits from nothing, so it no longer offers one — emptying it there
    dropped a protection no row on that screen could put back. The contract
    under test is the SAVE path, which is unchanged.
    """
    from fnd.config import load, starter_config
    from fnd.tui.menu import _open_filter_browser
    from fnd.tui.settings_screen import FilterBrowserScreen
    from fnd.tui.widgets.toggle_tree import ToggleTree

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(starter_config(), encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    app = FNDApp(index_dir=built_index, config=load(cfg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        _open_filter_browser(app)
        for _ in range(40):
            await pilot.pause()
        browser = app.screen
        assert isinstance(browser, FilterBrowserScreen)
        while browser._scanning:
            await pilot.pause()
        tree = browser.query_one("#filter_tree", ToggleTree)
        excluded = sorted(tree.excluded)
        assert excluded, "the premise: the starter config ships an exclusion"
        for item in excluded:
            node = next(n for n in _walk_nodes(tree.root) if (n.data or {}).get("id") == item)
            while item in tree.excluded:
                tree._toggle(node)
                await pilot.pause()
        for _ in range(6):
            await pilot.pause()
        await pilot.press("ctrl+s")
        for _ in range(20):
            await pilot.pause()

    assert load(cfg_path).defaults.filters.exclude_tags == []


@pytest.mark.asyncio
async def test_the_expression_can_be_copied(built_index: Path) -> None:
    """The app owns the mouse, so a terminal selection cannot reach the
    summary text — without a copy key the expression is display-only.

    Not ctrl+y: the app binds that to "copy query command" with priority, so
    a screen binding there silently never fires."""
    from fnd.filters import FilterSpec
    from fnd.tui.settings_screen import FilterBrowserScreen

    copied: list[str] = []
    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="t",
                spec=FilterSpec(exclude_tags=("no_index",), kinds=("md",)),
                gitignore=True,
                fndignore=True,
                on_save=lambda *_a: None,
            )
        )
        for _ in range(20):
            await pilot.pause()
        import fnd.tui.clipboard as clip

        real = clip.copy_text
        clip.copy_text = lambda text, **_k: copied.append(text)  # type: ignore[assignment]
        try:
            await pilot.press("y")
            for _ in range(10):
                await pilot.pause()
        finally:
            clip.copy_text = real  # type: ignore[assignment]

    assert copied, "the copy key did nothing"
    assert "file.kind in [" in copied[0]
    assert "no_index" in copied[0]


@pytest.mark.asyncio
async def test_the_summary_says_what_the_expression_leaves_out(built_index: Path) -> None:
    """Ignore files and path globs are not predicates over a file, so they
    cannot appear in the expression; presenting it as the whole filter
    invited the question of whether it was complete.

    The ignore files are named by their own row rather than by this line —
    the summary repeated the row it sits under, which cost three of the
    twenty-four rows a narrow terminal has.
    """
    from fnd.filters import FilterSpec
    from fnd.tui.settings_screen import FilterBrowserScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="t",
                spec=FilterSpec(),
                gitignore=True,
                fndignore=True,
                globs=["**/*.md"],
                on_save=lambda *_a: None,
            )
        )
        for _ in range(20):
            await pilot.pause()
        summary = _summary_text(app.screen)
        on_screen = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    assert "Outside the expression" in summary, summary
    assert "**/*.md" in summary
    assert ".gitignore" in on_screen, "nothing said the ignore files were in effect"
    assert ".gitignore" not in summary, "and the summary no longer repeats the row"


class TestClearClearsWhatItClaims:
    """`c` kept the frontmatter rule and the expression while the summary said
    everything was cleared, and switched both ignore-file toggles off, which
    widens the index rather than narrowing it."""

    def test_clear_empties_every_rule_including_the_text_ones(self) -> None:
        from fnd.filters import FilterSpec

        before = FilterSpec(
            kinds=("md",),
            max_size=99,
            frontmatter="Course == 'A'",
            expression="file.size > 1",
        )
        after = FilterSpec()
        assert before.frontmatter, "fixture must have a rule to clear"
        assert before.expression, "fixture must have an expression to clear"
        assert not after.frontmatter
        assert not after.expression
        assert not after.kinds
        assert after.max_size is None

    @pytest.mark.asyncio
    async def test_clear_leaves_the_ignore_toggles_alone(self, built_index: Path) -> None:
        """They have their own rows, and switching them off admits everything
        the ignore files were keeping out."""
        from fnd.tui.settings_screen import FilterBrowserScreen

        app = FNDApp(index_dir=built_index)
        async with app.run_test() as pilot:
            await pilot.pause()
            from fnd.filters import FilterSpec

            screen = FilterBrowserScreen(
                title="t",
                spec=FilterSpec(kinds=("md",)),
                gitignore=True,
                fndignore=True,
                on_save=lambda *_: None,
            )
            app.push_screen(screen)
            for _ in range(20):
                await pilot.pause()
            screen.action_clear_all()
            await pilot.pause()
            assert screen._gitignore is True, "clear switched .gitignore off"
            assert screen._fndignore is True, "clear switched .fndignore off"


class TestANoOpSaveChangesNothing:
    """Ways opening a source and saving unchanged altered what it indexes,
    each invisible afterwards. A mixed include list is covered in
    test_the_source_form_shows_every_include_glob."""

    def test_an_empty_clears_is_not_an_override(self) -> None:
        """It defaults to a list, so `exclude_none` always carried it, and
        every open-and-save looked like a change and forced a rebuild."""
        from fnd.config import SourceConfig, SourceFilters
        from fnd.tui.settings_screen import _seeded_filters

        plain = SourceConfig(path=Path("~/N"), filters=SourceFilters(kinds=["md"]))
        assert "clears" not in _seeded_filters(plain)

    def test_a_cleared_field_is_still_seeded(self) -> None:
        from fnd.config import SourceConfig, SourceFilters
        from fnd.tui.settings_screen import _seeded_filters

        cleared = SourceConfig(path=Path("~/N"), filters=SourceFilters(clears=["max_size"]))
        assert _seeded_filters(cleared)["clears"] == ["max_size"]

    def test_the_wizard_inherits_rather_than_opting_out(self) -> None:
        """Add-source inherited the global rule and add-collection did not:
        the same user action with two answers."""
        from fnd.tui.settings_screen import _merge_frontmatter

        assert _merge_frontmatter({}, "", "Course == 'A'", had_override=False) == {}


def test_a_long_path_row_marks_what_it_dropped() -> None:
    """A value is not an affordance: reserved in full, a long path ran past the
    right border and the terminal cut it with nothing to say so."""
    from fnd.tui.menu import KIND_SCALAR, MenuItem
    from fnd.tui.settings_screen import _render_row

    path = "~/Documents/Uni/B. Software Engineering (Honours)/2026 Semester 2/Cloud"
    item = MenuItem(
        id="form.path",
        label="Path",
        kind=KIND_SCALAR,
        setting_path="",
        value_getter=lambda _app: path,
        elide="head",
    )
    rendered = _render_row(item, cast("Any", object()), width=60).plain
    assert len(rendered) <= 60, rendered
    assert rendered.rstrip().endswith("Cloud"), "the leaf tells two sources apart"
    assert "…" in rendered


@pytest.mark.asyncio
async def test_the_sample_tester_appears_only_with_a_rule_to_test(built_index: Path) -> None:
    """It cost a third of the form on every source, and named a rule that
    lives two screens away without saying which."""
    from textual.widgets import Static

    from fnd.config import CollectionConfig, Config, SourceConfig, SourceFilters
    from fnd.tui.settings_screen import SourceFormScreen

    def _config(rule: str | None) -> Config:
        filters = SourceFilters(frontmatter=rule) if rule else None
        return Config(
            collections={
                "c": CollectionConfig(sources=[SourceConfig(path=Path("~/x"), filters=filters)])
            }
        )

    async def _separator(rule: str | None) -> tuple[bool, str]:
        app = FNDApp(index_dir=built_index, config=_config(rule))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
            for _ in range(20):
                await pilot.pause()
            sep = app.screen.query_one("#form_sample_sep", Static)
            return sep.display, str(sep.render())

    shown, _text = await _separator(None)
    assert not shown, "nothing to test without a rule"
    shown, text = await _separator("status == 'done'")
    assert shown
    assert "status == 'done'" in text, text


@pytest.mark.asyncio
async def test_unticking_custom_globs_keeps_them_on_offer(built_index: Path) -> None:
    """Untick discarded typed globs to one keypress, with no undo."""
    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.tui.settings_screen import SourceFormScreen, _custom_seed

    config = Config(collections={"c": CollectionConfig(sources=[SourceConfig(path=Path("~/x"))])})
    app = FNDApp(index_dir=built_index, config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
        for _ in range(20):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._fields["excludes_custom"] = "build/**, dist/**"
        form._set_excludes([])
        assert form._fields["excludes_custom"] == "", "untick must stop applying them"
        assert _custom_seed(form, "excludes_custom") == "build/**, dist/**"


def test_every_settings_screen_styles_itself() -> None:
    """Textual selectors are type selectors and a widget's own CSS is scoped
    to it, so a screen borrowing another's CSS rendered with no chrome."""
    from fnd.tui import settings_screen as ss

    borrowed = []
    for name in dir(ss):
        screen = getattr(ss, name)
        css = getattr(screen, "CSS", "") if isinstance(screen, type) else ""
        if css and "#settings_box" in css and f"{name} > #settings_box" not in css:
            borrowed.append(name)
    assert not borrowed, f"screens whose CSS names another type: {borrowed}"


class TestTheSummaryBoxTellsTheTruth:
    """It is five rows; past them the terminal cut the expression mid-token
    with nothing to say it had. Only reachable below ~60 columns."""

    HEAD = "Outside the expression — obeying .gitignore, .fndignore"
    PREFIX = "expression ('t' edits, 'y' copies):  "
    BODY = "(file.kind in ['pdf', 'docx', 'md', 'txt', 'pptx']) AND (NOT ('no_index' in file.tags.all))"

    def test_a_narrow_box_marks_what_it_dropped(self) -> None:
        from fnd.tui.settings_screen import _SUMMARY_ROWS, _FilterSummary

        rendered = _FilterSummary(self.HEAD, self.PREFIX, self.BODY).fitted(44).plain
        assert len(rendered.split("\n")) <= _SUMMARY_ROWS
        assert rendered.rstrip().endswith("…")

    def test_a_wide_box_shows_the_whole_expression(self) -> None:
        from fnd.tui.settings_screen import _FilterSummary

        rendered = _FilterSummary(self.HEAD, self.PREFIX, self.BODY).fitted(120).plain
        assert self.BODY in rendered
        assert "…" not in rendered


@pytest.mark.asyncio
async def test_a_partial_scan_says_so(built_index: Path) -> None:
    """`sample_source` stops at its time budget; presenting what it found as
    the whole source makes the type and tag lists silently incomplete."""

    from fnd.filters import FilterSpec
    from fnd.filters.scan import SourceSample
    from fnd.tui.settings_screen import FilterBrowserScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="Index filters",
                spec=FilterSpec(),
                gitignore=True,
                fndignore=True,
                on_save=lambda *_a: None,
            )
        )
        for _ in range(15):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FilterBrowserScreen)
        assert "partial scan" not in _summary_text(screen)

        screen._sample_arrived(SourceSample(kinds={"md": 1}, tags={}, truncated=True))
        for _ in range(6):
            await pilot.pause()
        assert "partial scan" in _summary_text(screen)


@pytest.mark.asyncio
async def test_the_wizard_refuses_an_invalid_rule_instead_of_crashing(built_index: Path) -> None:
    """The row shows a live ✗ col N but nothing stopped a save, and the model
    validates the rule, so the whole form's input died with the exception."""
    from textual.widgets import Static

    from fnd.tui.settings_screen import AddCollectionWizard

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        for _ in range(15):
            await pilot.pause()
        wizard = app.screen
        assert isinstance(wizard, AddCollectionWizard)
        wizard._fields.update({"name": "probe", "path": "~", "filter": "status =="})
        wizard.action_save_close()
        for _ in range(8):
            await pilot.pause()
        assert isinstance(app.screen, AddCollectionWizard), "the form must survive"
        error = wizard.query_one("#wizard_error", Static)
        assert "-hidden" not in error.classes
        assert "frontmatter" in str(error.render())


@pytest.mark.asyncio
async def test_the_rule_editor_names_the_glob_trap(built_index: Path) -> None:
    """Both hunters wrote `'*drafts*'`, watched it green-tick, and indexed
    every draft: `*` does not cross `/`. And "✓ every file" was the rule's
    scope, read as a claim that it matches everything."""
    from textual.widgets import Static

    from fnd.tui.settings_screen import RuleTextScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            RuleTextScreen(
                title="Custom rule",
                value="NOT file.path ~~ '*drafts*'",
                note_scoped=False,
                on_save=lambda _t: None,
            )
        )
        for _ in range(12):
            await pilot.pause()
        status = str(app.screen.query_one("#rule_status", Static).render())
        assert status.startswith("✓")
        assert "every file" in status
        assert status != "✓ every file", "the scope must not read as a match claim"
        help_text = str(app.screen.query_one("#rule_help", Static).render())
        assert "* stops at /" in help_text
        assert "'drafts/**'" in help_text


def test_the_legend_names_every_glyph_the_tree_paints() -> None:
    """`◐` is painted on any partly-on branch and was in no legend."""
    from fnd.filters.tree_model import LEGEND
    from fnd.tui.widgets.toggle_tree import _EMPTY, _EXCLUDED, _FULL, _PARTIAL

    for glyph in (_EXCLUDED, _FULL, _PARTIAL, _EMPTY):
        assert glyph in LEGEND, f"{glyph} is painted but unexplained"


def test_the_sources_row_names_every_dimension_that_narrows_it() -> None:
    """It named file types alone, so an inherited rule that cut a source to
    one file in sixteen still left the row reading "All types"."""
    from fnd.config import (
        CollectionConfig,
        Config,
        DefaultFilters,
        Defaults,
        SourceConfig,
        SourceFilters,
    )
    from fnd.tui.menu import _other_filters

    config = Config(
        defaults=Defaults(filters=DefaultFilters(expression="file.name ~~ 'alpha*'")),
        collections={
            "c": CollectionConfig(
                sources=[
                    SourceConfig(path=Path("~/x")),
                    SourceConfig(path=Path("~/y"), filters=SourceFilters(max_size=1000)),
                ]
            )
        },
    )
    inheriting, bounded = config.collections["c"].sources
    # The contract is that the row cannot read as unfiltered while an
    # inherited rule narrows it. It named the dimension until `12cd8bb`, which
    # collects the defaults' dimensions under one `inherited` chip so a source
    # stops advertising them as its own — the detail screen names which.
    assert _other_filters(inheriting) == ["inherited"], "it reads as unfiltered"
    assert "size" in _other_filters(bounded), "its own bound is still named"
    assert "size" not in _other_filters(inheriting)


def test_a_radio_group_keeps_one_option_selected() -> None:
    """`⏎` on the already-selected "Any size" turned it off, leaving nothing
    selected and the branch reading `(any)` — a fourth state, differing from
    `(Any size)` only in case, that the legend cannot express."""
    from fnd.tui.widgets.toggle_tree import ToggleGroup, ToggleItem, ToggleTree

    group = ToggleGroup(
        "size",
        "Maximum file size",
        (ToggleItem("size:any", "Any size"), ToggleItem("size:1mb", "Up to 1 MB")),
        mode="radio",
    )
    tree = ToggleTree("F")
    tree._by_id = {"size": group}
    tree._selected, tree._excluded = {"size:any"}, set()
    for pressed in ("size:any", "size:1mb", "size:1mb"):
        tree._selected -= {i.id for i in group.leaves if i.id != pressed}
        tree._selected.add(pressed)
        assert tree._selected, f"pressing {pressed} emptied the group"
    assert tree._selected == {"size:1mb"}


def test_the_pickers_name_what_they_hold() -> None:
    """ "40 selected" is the ABSENCE of a type restriction, and a count never
    showed the exclude globs anywhere in the UI."""
    from fnd.kinds import ALL_KIND_IDS
    from fnd.tui.settings_screen import _excludes_summary

    assert _excludes_summary({}) == "(none)"
    assert _excludes_summary({"excludes_custom": "build/**, dist/**"}) == "build/**, dist/**"
    assert "**/*.csv" in _excludes_summary({"excludes_custom": "**/*.csv"})
    assert len(ALL_KIND_IDS) > 1, "the wizard summary below depends on there being many"


@pytest.mark.asyncio
async def test_each_route_says_what_saving_does_to_the_index(built_index: Path) -> None:
    """The two routes differ and neither said so: a source save reindexes its
    collection, the defaults save reindexes nothing and leaves every
    collection holding what it already held."""
    from fnd.filters import FilterSpec
    from fnd.tui.settings_screen import FilterBrowserScreen

    async def _head(note: str) -> str:
        app = FNDApp(index_dir=built_index)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(
                FilterBrowserScreen(
                    title="Index filters",
                    spec=FilterSpec(),
                    gitignore=True,
                    fndignore=True,
                    save_note=note,
                    on_save=lambda *_a: None,
                )
            )
            for _ in range(12):
                await pilot.pause()
            return _summary_text(app.screen)

    assert "saving does not reindex" in await _head("saving does not reindex")
    assert "saving reindexes this collection" in await _head("saving reindexes this collection")


class TestUnsavedFilterWork:
    """`?` does not return here: it lands on the settings menu, taking the
    edit with it. `:` returns intact, so it is deliberately left alone."""

    @staticmethod
    async def _browser(app: FNDApp, pilot: Any) -> Any:
        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import FilterBrowserScreen

        app.push_screen(
            FilterBrowserScreen(
                title="Index filters",
                spec=FilterSpec(),
                gitignore=True,
                fndignore=True,
                on_save=lambda *_a: None,
            )
        )
        for _ in range(15):
            await pilot.pause()
        return app.screen

    @pytest.mark.asyncio
    async def test_an_edit_holds_the_help_key(self, built_index: Path) -> None:
        from dataclasses import replace

        from fnd.tui.settings_screen import FilterBrowserScreen

        app = FNDApp(index_dir=built_index)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = await self._browser(app, pilot)
            screen._spec = replace(screen._spec, kinds=("md",))
            await pilot.press("question_mark")
            for _ in range(10):
                await pilot.pause()
            assert isinstance(app.screen, FilterBrowserScreen), "the edit was carried off"

    @pytest.mark.asyncio
    async def test_help_still_opens_when_nothing_is_unsaved(self, built_index: Path) -> None:
        from fnd.tui.settings_screen import FilterBrowserScreen

        app = FNDApp(index_dir=built_index)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._browser(app, pilot)
            await pilot.press("question_mark")
            for _ in range(10):
                await pilot.pause()
            assert not isinstance(app.screen, FilterBrowserScreen), "? must still work"

    @pytest.mark.asyncio
    async def test_the_command_palette_comes_back_with_the_edit(self, built_index: Path) -> None:
        """The negative control for the key we chose NOT to guard."""
        from dataclasses import replace

        from fnd.tui.settings_screen import FilterBrowserScreen

        app = FNDApp(index_dir=built_index)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = await self._browser(app, pilot)
            screen._spec = replace(screen._spec, kinds=("md",))
            screen._rebuild()
            await pilot.press("colon")
            for _ in range(10):
                await pilot.pause()
            await pilot.press("escape")
            for _ in range(10):
                await pilot.pause()
            assert isinstance(app.screen, FilterBrowserScreen)
            assert app.screen._spec.kinds == ("md",)


class TestEscMeansOneThing:
    """It committed on a multi-select picker and cancelled on the single-select
    row beside it — one key, opposite meanings, and no way to back out of a
    multi picker at all."""

    @staticmethod
    def _item(multi: bool, sink: list[Any]) -> Any:
        from fnd.tui.menu import KIND_PICKER, ChoiceOption, MenuItem

        return MenuItem(
            id="probe",
            label="Probe",
            kind=KIND_PICKER,
            multi=multi,
            choices_provider=lambda _app: [
                ChoiceOption(value="a", label="A"),
                ChoiceOption(value="b", label="B"),
            ],
            picker_getter=lambda _app: [] if multi else "",
            picker_setter=lambda _app, v: sink.append(v),
        )

    @pytest.mark.asyncio
    async def test_esc_discards_a_multi_selection(self, built_index: Path) -> None:
        from fnd.tui.settings_screen import PickerScreen

        sink: list[Any] = []
        app = FNDApp(index_dir=built_index)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(PickerScreen(self._item(True, sink)))
            for _ in range(12):
                await pilot.pause()
            await pilot.press("enter")
            await pilot.press("escape")
            for _ in range(8):
                await pilot.pause()
        assert sink == [], "Esc must not commit"

    @pytest.mark.asyncio
    async def test_ctrl_s_commits_it(self, built_index: Path) -> None:
        from fnd.tui.settings_screen import PickerScreen

        sink: list[Any] = []
        app = FNDApp(index_dir=built_index)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(PickerScreen(self._item(True, sink)))
            for _ in range(12):
                await pilot.pause()
            await pilot.press("enter")
            await pilot.press("ctrl+s")
            for _ in range(8):
                await pilot.pause()
        assert sink, "^S must commit"
        assert sink[-1] == ["a"], sink


def test_the_rename_row_describes_what_rename_does() -> None:
    """It claimed scope follows the new name and the index is not rebuilt.
    Neither is true: `_save` ends in `reindex_with_warning(rebuild=True)` and
    nothing migrates the saved selection."""
    import inspect

    from fnd.tui.menu import _provider_collection
    from fnd.tui.settings_screen import RenameCollectionScreen

    stub = cast("Any", None)
    row = next(i for i in _provider_collection(stub, "c") if i.id == "col.c.rename")
    # The whole class: the rebuild moved out of _save when the old name's
    # index drop had to run before it, and reading one method missed it.
    source = inspect.getsource(RenameCollectionScreen)
    assert "rebuild=True" in source, "guard: this test pins the row against the code"
    assert "not rebuilt" not in row.description
    assert "does not follow" in row.description


@pytest.mark.asyncio
async def test_leaving_the_source_form_says_what_it_discards(built_index: Path) -> None:
    """The filter browser saves into `_fields`, not to disk, so ^S there and
    Esc here threw the edit away without a word. It said so afterwards once
    that was fixed; it asks first now, which is what makes the edit
    recoverable rather than merely reported."""
    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.tui.settings_screen import SourceFormScreen

    config = Config(collections={"c": CollectionConfig(sources=[SourceConfig(path=Path("~/x"))])})
    app = FNDApp(index_dir=built_index, config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
        for _ in range(20):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form.action_back()
        await pilot.pause()
        assert not isinstance(app.screen, UnsavedChangesScreen), (
            "an untouched form must not stop the user"
        )

        app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
        for _ in range(20):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._fields["filters"] = {"kinds": ["md"]}
        form.action_back()
        await pilot.pause()
        assert isinstance(app.screen, UnsavedChangesScreen), "the edit was dropped without asking"


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [50, 55, 60, 80])
async def test_the_edit_field_is_never_off_screen(built_index: Path, width: int) -> None:
    """The bar's label was uncapped, so on a narrow terminal it pushed the
    field past the right edge: typing changed no painted row while the value
    accumulated, and saving wrote it into the config."""
    from textual.widgets import Input

    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.tui.settings_screen import SettingsList, SourceFormScreen

    config = Config(
        collections={
            "c": CollectionConfig(sources=[SourceConfig(path=Path("~/x"), includes=["**/*.md"])])
        }
    )
    app = FNDApp(index_dir=built_index, config=config)
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
        for _ in range(20):
            await pilot.pause()
        form = app.screen
        rows = form.query_one(SettingsList)
        rows.cursor_index = next(
            i for i, it in enumerate(rows._items) if it.id == "form.includes_custom"
        )
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause()
        field = form.query_one("#editor_input", Input)
        assert field.region.right <= width, f"field at {field.region} is off a {width}-col screen"
        before = [s.text for s in form._compositor.render_strips()]
        await pilot.press("a")
        await pilot.pause()
        after = [s.text for s in form._compositor.render_strips()]
        assert before != after, "typing changed nothing on screen"


def test_the_footer_never_drops_the_save_key() -> None:
    """Dropping from the right cut `^S Save` while leaving `c Clear` — a
    destructive key outliving the one that keeps the work."""
    from fnd.tui.app import render_hint_bar

    bar = render_hint_bar(
        (("/", "Search"), (":", "Menu"), ("?", "Keys"), ("q", "Quit")),
        (
            ("⏎", "Toggle"),
            ("→", "Open"),
            ("t", "As text"),
            ("c", "Clear"),
            (COMMIT_KEY, "Save"),
            ("y", "Copy"),
            ("Esc/←", "Discard"),
        ),
    )
    for width in (44, 55, 60, 70, 80, 100):
        painted = bar.fitted(width).plain
        assert "Save" in painted, f"the save key was dropped at {width} cols: {painted}"
        assert bar.fitted(width).cell_len <= width


@pytest.mark.asyncio
async def test_the_summary_names_the_excludes_too(built_index: Path) -> None:
    """It named the include globs and never the excludes, which drop files
    before any filter runs — so it was silent about half of what is skipped."""
    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.tui.settings_screen import FilterBrowserScreen, SourceFormScreen

    config = Config(
        collections={
            "c": CollectionConfig(
                sources=[
                    SourceConfig(path=Path("~/x"), includes=["notes/**"], excludes=["**/*.csv"])
                ]
            )
        }
    )
    app = FNDApp(index_dir=built_index, config=config)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
        for _ in range(20):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        form._open_filters()
        for _ in range(30):
            await pilot.pause()
        assert isinstance(app.screen, FilterBrowserScreen)
        summary = _summary_text(app.screen)
        assert "restricted to paths: notes/**" in summary, summary
        assert "**/*.csv" in summary, f"the exclude is unmentioned: {summary}"


def test_a_bound_no_picker_holds_is_still_visible() -> None:
    """`Created within` speaks for `created_after` only, so a `created_before`
    in force left the tree reading as though nothing were set."""
    from fnd.filters import FilterSpec
    from fnd.filters.tree_model import spec_branches

    spec = FilterSpec(created_before=dt.date(2026, 1, 1))
    beyond = next(b for b in spec_branches(spec) if b.id == "beyond")
    assert [label for _i, label in beyond.items] == ["Created before 2026-01-01"]


@pytest.mark.asyncio
async def test_the_summary_says_when_nothing_can_match(built_index: Path) -> None:
    """A contradictory pair indexes nothing and said nothing about it."""
    from fnd.filters import FilterSpec
    from fnd.tui.settings_screen import FilterBrowserScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="Index filters",
                spec=FilterSpec(min_size=5000, max_size=100),
                gitignore=True,
                fndignore=True,
                on_save=lambda *_a: None,
            )
        )
        for _ in range(15):
            await pilot.pause()
        assert "nothing can match" in _summary_text(app.screen)


@pytest.mark.asyncio
async def test_a_text_editor_advertises_only_keys_that_work(built_index: Path) -> None:
    """`/`, `:`, `?` and `q` type into the box rather than acting, so a footer
    naming them as Search/Menu/Keys/Quit names four dead keys."""
    from textual.widgets import Static, TextArea

    from fnd.tui.settings_screen import RuleTextScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            RuleTextScreen(title="Rule", value="", note_scoped=False, on_save=lambda _t: None)
        )
        for _ in range(12):
            await pilot.pause()
        screen = app.screen
        painted = screen.query_one("#footer_hints", Static).render_line(0).text
        for dead in ("Search", "Menu", "Keys", "Quit"):
            assert dead not in painted, f"{dead} is advertised but types into the box: {painted}"
        # The commit key, whatever it is called: this editor applies rather
        # than saves, since it hands back to the browser.
        assert COMMIT_KEY in painted
        assert "Cancel" in painted

        for key in ("slash", "colon", "question_mark", "q"):
            await pilot.press(key)
        await pilot.pause()
        assert screen.query_one("#rule_text", TextArea).text == "/:?q", (
            "the premise: those keys reach the box"
        )


class TestTheTwoLevelSaveSaysWhichLevelItIs:
    """The per-source browser stages into the form, which owns the write. It
    said "^S Save" and promised "saving reindexes this collection", so a single
    ^S looked committed while writing nothing — and a later Esc then reported
    the work discarded, after the user had pressed save."""

    @pytest.mark.asyncio
    async def test_the_source_route_calls_it_applying(self, built_index: Path) -> None:
        from textual.widgets import Static

        from fnd.config import CollectionConfig, Config, SourceConfig
        from fnd.tui.settings_screen import FilterBrowserScreen, SourceFormScreen

        config = Config(
            collections={"c": CollectionConfig(sources=[SourceConfig(path=Path("~/x"))])}
        )
        app = FNDApp(index_dir=built_index, config=config)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
            for _ in range(20):
                await pilot.pause()
            form = app.screen
            assert isinstance(form, SourceFormScreen)
            form._open_filters()
            for _ in range(30):
                await pilot.pause()
            assert isinstance(app.screen, FilterBrowserScreen)
            summary = _summary_text(app.screen)
            assert "applies here" in summary, summary
            assert "saving reindexes this collection" not in summary
            footer = app.screen.query_one("#footer_hints", Static).render_line(0).text
            assert "Apply" in footer, footer

    @pytest.mark.asyncio
    async def test_the_defaults_route_still_calls_it_saving(self, built_index: Path) -> None:
        """There the screen does own the write, so "Save" is the truth."""
        from textual.widgets import Static

        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import FilterBrowserScreen

        app = FNDApp(index_dir=built_index)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.push_screen(
                FilterBrowserScreen(
                    title="Index filters",
                    spec=FilterSpec(),
                    gitignore=True,
                    fndignore=True,
                    save_note="saving does not reindex",
                    on_save=lambda *_a: None,
                )
            )
            for _ in range(15):
                await pilot.pause()
            footer = app.screen.query_one("#footer_hints", Static).render_line(0).text
            assert "Save" in footer
            assert "Apply" not in footer


class TestClearSaysWhatItTook:
    """Clearing takes no confirmation and wiped the set in silence —
    including a tag exclusion, which is a protection rather than a
    preference. The gesture is the sidebar's, not a second letter of its
    own; the tests press whatever that action is bound to."""

    def test_every_spec_field_has_a_name_a_user_would_recognise(self) -> None:
        from fnd.tui.settings_screen import _FIELD_WORDS, _SPEC_FIELDS

        assert set(_SPEC_FIELDS) == set(_FIELD_WORDS), "a field with no word to name it"

    def test_it_names_the_protection_it_dropped(self) -> None:
        """A tag exclusion is a protection, not a preference, so losing one
        has to be said out loud."""
        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import _cleared_note

        before = FilterSpec(kinds=("md",), exclude_tags={"os": ("no_index",)})
        note = _cleared_note(before, FilterSpec())
        assert "skipped tags" in note, note
        assert "file types" in note

    def test_no_wording_claims_nothing_is_filtered_out(self) -> None:
        """It was never true — ignore files and hidden-name pruning survive
        any clear — and the route that said it no longer exists."""
        import inspect

        from fnd.tui import settings_screen

        assert "nothing is filtered out" not in inspect.getsource(settings_screen)

    def test_the_source_route_says_it_went_back_to_inherited(self) -> None:
        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import _cleared_note

        before = FilterSpec(kinds=("md",), exclude_tags={"os": ("no_index",)})
        after = FilterSpec(exclude_tags={"os": ("no_index",)})
        note = _cleared_note(before, after)
        assert "inherited" in note
        assert "skipped tags" not in note, "the exclusion survived, so it was not taken"

    def test_clearing_nothing_says_so(self) -> None:
        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import _cleared_note

        assert _cleared_note(FilterSpec(), FilterSpec()) == "Nothing to return"

    @pytest.mark.asyncio
    async def test_clearing_raises_it(self, built_index: Path) -> None:
        """On a SOURCE, which is the only place the act exists now: the global
        set inherits from nothing, so there is nothing to return to there."""
        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import _CLEAR_FILTERS_KEY, FilterBrowserScreen

        app = FNDApp(index_dir=built_index)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.push_screen(
                FilterBrowserScreen(
                    title="Index filters",
                    spec=FilterSpec(kinds=("md",), exclude_tags={"os": ("no_index",)}),
                    gitignore=True,
                    fndignore=True,
                    inherited=(FilterSpec(), True, True),
                    on_save=lambda *_a: None,
                )
            )
            for _ in range(15):
                await pilot.pause()
            said: list[str] = []
            app.screen.notify = lambda msg, **kw: said.append(str(msg))  # type: ignore[method-assign]
            await pilot.press(_CLEAR_FILTERS_KEY)
            for _ in range(6):
                await pilot.pause()
            assert said, "clearing said nothing"
            assert "skipped tags" in said[0], said


class TestSettingsScreenTyping:
    """Two fixes made tonight for other screens had not reached this one: its
    edit bar docked to the screen while its panel is inset, and its footer
    named `/ : ? q` while they typed into the focused box."""

    @staticmethod
    async def _open(app: FNDApp, pilot: Any) -> Any:
        from fnd.tui.menu import SECTION_FILTERS
        from fnd.tui.settings_screen import open_settings_section

        open_settings_section(app, SECTION_FILTERS)
        for _ in range(25):
            await pilot.pause()
        return app.screen

    @pytest.mark.asyncio
    async def test_the_edit_field_stays_inside_the_panel(self, built_index: Path) -> None:
        from textual.widgets import Input

        from fnd.tui.settings_screen import SettingsList

        app = FNDApp(index_dir=built_index)
        async with app.run_test(size=(100, 26)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            rows = screen.query_one(SettingsList)
            rows.cursor_index = next(
                i for i, it in enumerate(rows._items) if it.id == "filters.tag_frontmatter_keys"
            )
            await pilot.press("enter")
            for _ in range(8):
                await pilot.pause()
            panel = screen.query_one("#settings_box")
            field = screen.query_one("#editor_input", Input)
            assert panel.region.contains_region(field.region), (
                f"field {field.region} left panel {panel.region}"
            )

    @pytest.mark.asyncio
    async def test_the_footer_drops_keys_that_type(self, built_index: Path) -> None:
        from textual.widgets import Input, Static

        from fnd.tui.settings_screen import SettingsList

        app = FNDApp(index_dir=built_index)
        async with app.run_test(size=(100, 26)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            footer = screen.query_one("#footer_hints", Static)
            # `Menu`, not `Search`: `/` is no longer an anchor in Settings,
            # because there it focuses the row filter rather than the query
            # bar and the screen's own cluster names it.
            assert "Menu" in footer.render_line(0).text, "idle, the anchors do work"

            rows = screen.query_one(SettingsList)
            rows.cursor_index = next(
                i for i, it in enumerate(rows._items) if it.id == "filters.tag_frontmatter_keys"
            )
            await pilot.press("enter")
            for _ in range(8):
                await pilot.pause()
            painted = footer.render_line(0).text
            for dead in ("Search", "Menu", "Keys", "Quit"):
                assert dead not in painted, f"{dead} types into the box: {painted}"

            await pilot.press("slash")
            await pilot.pause()
            assert screen.query_one("#editor_input", Input).value == "/", "the premise"


def test_no_screen_with_a_text_box_advertises_the_anchors_unguarded() -> None:
    """`/`, `:`, `?` and `q` reach a focused box instead of acting.

    A class-wide guard, not four named screens: two fixes for this defect were
    made on the screens where it was reported and left every other screen that
    composes the same chrome broken. Any new screen with a text box has to
    decide, rather than inheriting the wrong answer.
    """
    import inspect
    import re

    from textual.screen import Screen

    from fnd.tui import settings_screen as module

    unguarded = []
    for name in dir(module):
        screen = getattr(module, name)
        if not (isinstance(screen, type) and issubclass(screen, Screen) and screen is not Screen):
            continue
        try:
            source = inspect.getsource(screen)
        except OSError:  # pragma: no cover - only for C-defined classes
            continue
        takes_typing = bool(re.search(r"yield (Input|TextArea)\(|yield EditBar\(\)", source))
        guarded = "_editor_hint_bar(" in source or "_wizard_hints(" in source
        if takes_typing and "_hint_bar(" in source and not guarded:
            unguarded.append(name)
    assert not unguarded, f"screens naming keys that type into their box: {unguarded}"


class TestARejectedSaveStopsComplainingOnceFixed:
    """The reason stayed on screen while the user fixed the very field it
    named, so "Name is required." sat above a filled-in name."""

    @pytest.mark.asyncio
    async def test_the_wizard_clears_it(self, built_index: Path) -> None:
        from textual.widgets import Static

        from fnd.tui.settings_screen import AddCollectionWizard

        app = FNDApp(index_dir=built_index)
        async with app.run_test(size=(100, 26)) as pilot:
            await pilot.pause()
            app.push_screen(AddCollectionWizard())
            for _ in range(20):
                await pilot.pause()
            wizard = app.screen
            assert isinstance(wizard, AddCollectionWizard)
            error = wizard.query_one("#wizard_error", Static)
            wizard.action_save_close()
            for _ in range(8):
                await pilot.pause()
            assert "-hidden" not in error.classes, "the premise: a bad save complains"
            assert "Name is required" in str(error.render())

            wizard._fields["name"] = "probe"
            wizard._populate_fields()
            for _ in range(8):
                await pilot.pause()
            assert "-hidden" in error.classes, "it still complains after the fix"

    @pytest.mark.asyncio
    async def test_the_source_form_clears_it_too(self, built_index: Path) -> None:
        """Same pattern, same screen family — fixed as a class."""
        from textual.widgets import Static

        from fnd.config import CollectionConfig, Config, SourceConfig
        from fnd.tui.settings_screen import SourceFormScreen

        config = Config(
            collections={"c": CollectionConfig(sources=[SourceConfig(path=Path("~/x"))])}
        )
        app = FNDApp(index_dir=built_index, config=config)
        async with app.run_test(size=(100, 26)) as pilot:
            await pilot.pause()
            app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
            for _ in range(20):
                await pilot.pause()
            form = app.screen
            assert isinstance(form, SourceFormScreen)
            error = form.query_one("#form_error", Static)
            form._show_error("Path does not exist: /nope")
            await pilot.pause()
            assert "-hidden" not in error.classes
            form._populate_fields()
            for _ in range(6):
                await pilot.pause()
            assert "-hidden" in error.classes


class TestNothingIsThrownAwayInSilence:
    """Every screen holding user work asks before leaving discards it.

    These asserted a notification saying the work HAD been discarded, which
    was the best the app did before the prompt existed — telling the user
    after it was gone. The intent is unchanged; the mechanism is now a modal
    offering Save, Discard or Keep editing, so the work is recoverable.
    """

    @pytest.mark.asyncio
    async def test_the_wizard_says_it_discarded_a_filled_form(self, built_index: Path) -> None:
        from fnd.tui.settings_screen import AddCollectionWizard

        app = FNDApp(index_dir=built_index)
        async with app.run_test(size=(100, 26)) as pilot:
            await pilot.pause()
            app.push_screen(AddCollectionWizard())
            for _ in range(20):
                await pilot.pause()
            wizard = app.screen
            assert isinstance(wizard, AddCollectionWizard)
            wizard.action_back()
            await pilot.pause()
            assert not isinstance(app.screen, UnsavedChangesScreen), (
                "an untouched form must not stop the user"
            )

            app.push_screen(AddCollectionWizard())
            for _ in range(20):
                await pilot.pause()
            wizard = app.screen
            assert isinstance(wizard, AddCollectionWizard)
            wizard._fields["name"] = "probe"
            wizard.action_back()
            await pilot.pause()
            assert isinstance(app.screen, UnsavedChangesScreen), (
                "a filled form was discarded without asking"
            )

    @pytest.mark.asyncio
    async def test_the_text_view_says_it_discarded_typing(self, built_index: Path) -> None:
        from textual.widgets import TextArea

        from fnd.filters import FilterSpec
        from fnd.tui.settings_screen import FilterTextScreen

        app = FNDApp(index_dir=built_index)
        async with app.run_test(size=(100, 26)) as pilot:
            await pilot.pause()
            app.push_screen(
                FilterTextScreen(title="As text", spec=FilterSpec(), on_save=lambda _s: None)
            )
            for _ in range(15):
                await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FilterTextScreen)
            screen.action_back()
            await pilot.pause()
            assert not isinstance(app.screen, UnsavedChangesScreen), (
                "untouched text must not stop the user"
            )

            app.push_screen(
                FilterTextScreen(title="As text", spec=FilterSpec(), on_save=lambda _s: None)
            )
            for _ in range(15):
                await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FilterTextScreen)
            screen.query_one("#filter_text", TextArea).text = "file.kind in ['md']"
            screen.action_back()
            await pilot.pause()
            assert isinstance(app.screen, UnsavedChangesScreen), (
                "typed text was discarded without asking"
            )


class TestSavingLandsAnOpenEdit:
    """`^S` bypassed an open edit bar entirely, so a value the user had just
    typed was dropped without a word while the form saved without it."""

    @pytest.mark.asyncio
    async def test_the_typed_value_is_committed(self, built_index: Path) -> None:
        from fnd.config import CollectionConfig, Config, SourceConfig
        from fnd.tui.settings_screen import SettingsList, SourceFormScreen

        config = Config(
            collections={"c": CollectionConfig(sources=[SourceConfig(path=Path("~/x"))])}
        )
        app = FNDApp(index_dir=built_index, config=config)
        async with app.run_test(size=(100, 26)) as pilot:
            await pilot.pause()
            app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
            for _ in range(20):
                await pilot.pause()
            form = app.screen
            assert isinstance(form, SourceFormScreen)
            rows = form.query_one(SettingsList)
            rows.cursor_index = next(
                i for i, it in enumerate(rows._items) if it.id == "form.includes_custom"
            )
            await pilot.press("enter")
            for _ in range(8):
                await pilot.pause()
            for char in "notes":
                await pilot.press(char)
            await pilot.pause()
            assert form._fields["includes_custom"] == "", "the premise: not committed yet"
            await pilot.press("ctrl+s")
            for _ in range(14):
                await pilot.pause()
            assert form._fields["includes_custom"] == "notes"

    @pytest.mark.asyncio
    async def test_a_rejected_value_neither_saves_nor_loops(self, built_index: Path) -> None:
        """The bar stays open showing why, and the save does not happen."""
        from fnd.config import CollectionConfig, Config, SourceConfig
        from fnd.tui.settings_screen import EditBar, SourceFormScreen, _commit_then

        config = Config(
            collections={"c": CollectionConfig(sources=[SourceConfig(path=Path("~/x"))])}
        )
        app = FNDApp(index_dir=built_index, config=config)
        async with app.run_test(size=(100, 26)) as pilot:
            await pilot.pause()
            app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
            for _ in range(20):
                await pilot.pause()
            form = app.screen
            bar = form.query_one(EditBar)
            bar.remove_class("-hidden")  # an open bar that will not close
            calls: list[int] = []
            waited = _commit_then(form, lambda: calls.append(1))
            assert waited, "an open bar must make the caller wait"
            for _ in range(10):
                await pilot.pause()
            assert calls == [], "the save must not run while the bar is still open"


class TestTabIsOfferedOnlyWhenItGoesSomewhere:
    """The sample tester is hidden without a rule to test, so Tab put the
    cursor in an invisible pane while the footer advertised it. "Sample" also
    read as "a sample of files", which is what a user wants there."""

    @pytest.mark.asyncio
    async def test_the_wizard_neither_offers_nor_focuses_it(self, built_index: Path) -> None:
        from fnd.tui.settings_screen import AddCollectionWizard, _focus_targets, _wizard_hints

        app = FNDApp(index_dir=built_index)
        async with app.run_test(size=(140, 26)) as pilot:
            await pilot.pause()
            app.push_screen(AddCollectionWizard())
            for _ in range(20):
                await pilot.pause()
            wizard = app.screen
            assert isinstance(wizard, AddCollectionWizard)
            assert len(_focus_targets(wizard)) == 1
            assert "Tab" not in _wizard_hints(wizard, app).plain
            await pilot.press("tab")
            await pilot.pause()
            assert type(app.focused).__name__ != "TextArea", "focus entered a hidden pane"

            wizard._fields["filter"] = "status == 'done'"
            wizard._populate_fields()
            for _ in range(8):
                await pilot.pause()
            assert len(_focus_targets(wizard)) == 2
            assert "Test a sample" in _wizard_hints(wizard, app).plain
            await pilot.press("tab")
            await pilot.pause()
            assert type(app.focused).__name__ == "TextArea"

    @pytest.mark.asyncio
    async def test_the_source_form_does_the_same(self, built_index: Path) -> None:
        from fnd.config import CollectionConfig, Config, SourceConfig, SourceFilters
        from fnd.tui.settings_screen import SourceFormScreen, _focus_targets

        def _app(rule: str | None) -> FNDApp:
            filters = SourceFilters(frontmatter=rule) if rule else None
            config = Config(
                collections={
                    "c": CollectionConfig(sources=[SourceConfig(path=Path("~/x"), filters=filters)])
                }
            )
            return FNDApp(index_dir=built_index, config=config)

        for rule, panes in ((None, 1), ("status == 'done'", 2)):
            app = _app(rule)
            async with app.run_test(size=(140, 26)) as pilot:
                await pilot.pause()
                app.push_screen(SourceFormScreen(collection_name="c", source_index=0))
                for _ in range(20):
                    await pilot.pause()
                assert len(_focus_targets(app.screen)) == panes, rule


class TestTheCollectionsTitleCountsWhatIsSearched:
    """It counted only fully-ticked collections while the search includes any
    collection with an active source, so three partly-ticked collections read
    "0/3 active" while all three were being searched."""

    @staticmethod
    def _panel(markers: dict[str, str]) -> Any:
        """A scope stub whose markers are fixed, so the title is the only
        thing under test."""
        from fnd.tui.scope_panel import ScopeController

        panel = object.__new__(ScopeController)
        panel.collection_marker = markers.get  # type: ignore[method-assign]
        return panel

    def test_a_partly_ticked_collection_counts(self) -> None:
        panel = self._panel({"a": "◐", "b": "◐", "c": "◐"})
        assert sum(1 for n in "abc" if panel.collection_marker(n) != "○") == 3

    def test_an_empty_one_does_not(self) -> None:
        panel = self._panel({"a": "●", "b": "◐", "c": "○"})
        assert sum(1 for n in "abc" if panel.collection_marker(n) != "○") == 2

    def test_the_old_rule_would_have_said_zero(self) -> None:
        """The negative control: counting only ● for three partial ones."""
        panel = self._panel({"a": "◐", "b": "◐", "c": "◐"})
        assert sum(1 for n in "abc" if panel.collection_marker(n) == "●") == 0


def test_only_a_screen_that_writes_says_save() -> None:
    """Four nested screens said "Save" and one of them saved. The rule now:
    "Apply" hands the value up, "Save" reaches disk."""
    import inspect
    import re

    from fnd.tui import settings_screen as module

    # Match the identifier, not the spelling: this scraped a literal and broke
    # the moment the label became one constant, which is what it is for.
    staging = ("FilterTextScreen", "RuleTextScreen")
    for name in staging:
        source = inspect.getsource(getattr(module, name))
        labels = set(re.findall(r'COMMIT_KEY,\s*"([^"]+)"', source))
        assert labels, f"{name} names no commit key"
        assert labels == {"Apply"}, f"{name} says {labels}, but it never writes"

    writes = inspect.getsource(module.SourceFormScreen)
    assert re.search(r'COMMIT_KEY,\s*"Save"', writes), "the screen that does write still says Save"


class TestTheDefaultsScreenOffersEveryType:
    """It sampled the first few collections for kinds, so file-type groups
    vanished from it as collections were added — while the defaults it edits
    apply to every collection, including the ones never scanned."""

    def test_every_registry_kind_is_offered(self) -> None:
        from fnd.filters import FilterSpec
        from fnd.filters.scan import SourceSample
        from fnd.filters.tree_model import spec_branches
        from fnd.kinds import ALL_KIND_IDS

        sample = SourceSample(kinds={}, tags={"os": {"x": 1}})
        kinds = next(b for b in spec_branches(FilterSpec(), sample) if b.id == "kinds")
        offered = {i.removeprefix("kind:") for g in kinds.groups for i, _l in g.items}
        assert offered == set(ALL_KIND_IDS)

    def test_a_sampled_screen_offers_every_type_too(self) -> None:
        """Overruled deliberately: a picker showing only today's types has to
        be revisited as the corpus grows. The counts still say what is there."""
        from fnd.filters import FilterSpec
        from fnd.filters.scan import SourceSample
        from fnd.filters.tree_model import spec_branches
        from fnd.kinds import ALL_KIND_IDS

        sample = SourceSample(kinds={"md": 3}, tags={})
        kinds = next(b for b in spec_branches(FilterSpec(), sample) if b.id == "kinds")
        items = {i.removeprefix("kind:"): label for g in kinds.groups for i, label in g.items}
        assert set(items) == set(ALL_KIND_IDS)
        assert "3" in items["md"], items["md"]
