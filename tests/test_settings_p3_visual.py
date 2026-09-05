"""Settings UX redesign — visual foundation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._pilot_wait import settings_ready, wait_until


def test_indexer_filetypes_exposed_and_complete() -> None:
    """Spec: Add Collection wizard › Includes — file types come from a
    single source of truth, not hardcoded in two places."""
    from fnd.config import INDEXER_FILETYPES
    from fnd.kinds import ALL_KIND_IDS

    # Kind id -> human label, derived from the single registry source of truth.
    assert tuple(INDEXER_FILETYPES) == tuple(ALL_KIND_IDS)
    # The original document kinds are still present with descriptive labels…
    assert INDEXER_FILETYPES["md"] == "Markdown (.md/.markdown)"
    assert INDEXER_FILETYPES["pdf"] == "PDF (.pdf)"
    # …and the broadened set now includes the new families.
    for kind_id in ("epub", "python", "csv", "html", "ipynb", "odt"):
        assert kind_id in INDEXER_FILETYPES


def test_f3_no_longer_in_keymap() -> None:
    """Spec: Locked decisions — F3 dropped."""
    from fnd.tui.actions import load_keymap

    keymap = load_keymap()
    assert "f3" not in keymap.bindings, (
        f"F3 should not be bound; keymap.bindings has: {keymap.bindings.get('f3')!r}"
    )


def test_detail_strip_renders_description_and_metadata() -> None:
    """Spec: Visual system › Detail strip — 2 lines, description then
    metadata in $text-muted."""
    from fnd.tui.widgets.detail_strip import DetailStrip

    strip = DetailStrip()
    strip._description = "Result limit (1–1000) — max results returned per query."
    strip._metadata = "Stored in defaults.result_limit · Applies on next search"
    rendered = strip._render_lines()
    assert len(rendered) == 2
    assert "Result limit" in str(rendered[0])
    assert "Stored in defaults.result_limit" in str(rendered[1])


def test_row_with_key_renders_bracketed_accent() -> None:
    """Spec: Visual system › Key style — bracketed `[o]` accent."""
    from fnd.tui.menu import KIND_ACTION, MenuItem
    from fnd.tui.settings_screen import _render_row

    item = MenuItem(
        id="k.test",
        label="Open at locator",
        kind=KIND_ACTION,
        key="o",
        action_id="open_at_locator",
    )
    rendered = _render_row(item, app=None, width=80)
    text_str = str(rendered)
    assert "[o]" in text_str, f"expected '[o]' in rendered row; got: {text_str!r}"
    assert "▶" not in text_str


def test_root_container_hugs_content() -> None:
    """Spec: Visual system › Container — height: auto, not 1fr."""
    from fnd.tui.settings_screen import SettingsScreen

    css = SettingsScreen.CSS
    # Find the #settings_box rule and check its height.
    box_rule = css.split("#settings_box {")[1].split("}")[0]
    assert "height: auto" in box_rule
    assert "max-height" in box_rule
    assert "align: center middle" in css  # somewhere in the screen styles


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from fnd.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_root_rows_show_trailing_summaries(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: IA › Root — every drill row shows what's inside (always_show mode)."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    # Isolate from the user's real config so drill_summary_mode stays at
    # the default "always_show" regardless of what is on disk.
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        by_label = {it.label: it for it in lst._items}
        preferences = by_label["Preferences"]
        assert preferences.trailing_value(app), "Preferences row needs a trailing summary"
        collections = by_label["Collections"]
        assert "collection" in collections.trailing_value(app).lower()
        keybindings = by_label["Keybindings"]
        assert "key" in keybindings.trailing_value(app).lower()


@pytest.mark.asyncio
async def test_collection_row_shows_source_count_and_ranking(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: IA › Collections sub-screen — each collection row's trailing
    shows source count and `ranking:<profile>` with scope dot prefix."""
    from fnd.config import (
        CollectionConfig,
        SourceConfig,
        write_collection,
    )
    from fnd.tui import FNDApp
    from fnd.tui.menu import SECTION_COLLECTIONS, section_items

    # Isolate config so we have a known "default" collection.
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    real = tmp_path / "docs"
    real.mkdir()
    write_collection(
        config_path=cfg_path,
        name="default",
        collection=CollectionConfig(sources=[SourceConfig(path=real, includes=["**/*.md"])]),
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test():
        from fnd.config import load

        app._config = load()  # type: ignore[attr-defined]
        items = section_items(app, SECTION_COLLECTIONS)
        default = next(it for it in items if it.id == "collection.default")
        trailing = default.trailing_value(app)
        assert "source" in trailing.lower()
        assert "ranking" in trailing.lower()
        # Scope dot ● or ○ is rendered at the start.
        assert trailing[0] in ("●", "○")


@pytest.mark.asyncio
async def test_source_row_shows_filetypes_and_path_warning(
    tmp_path: Path, built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: IA › Sources sub-screen — source rows show file-types and
    `⚠ path not found` when the path no longer resolves."""
    from fnd.config import (
        CollectionConfig,
        SourceConfig,
        write_collection,
    )
    from fnd.tui import FNDApp
    from fnd.tui.menu import _provider_sources

    # Isolate config writes.
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    # Make a collection with two sources: one valid, one missing.
    real = tmp_path / "exists"
    real.mkdir()
    (real / "a.md").write_text("x")
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(
            sources=[
                SourceConfig(path=real, includes=["**/*.md"]),
                SourceConfig(path=tmp_path / "nope", includes=["**/*.pdf"]),
            ]
        ),
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test():
        # Reload config so the new collection is visible.
        from fnd.config import load

        app._config = load()  # type: ignore[attr-defined]
        items = _provider_sources(app, "probe")
        valid = next(it for it in items if it.id == "sources.probe.0")
        missing = next(it for it in items if it.id == "sources.probe.1")
        assert "md" in valid.trailing_value(app).lower()
        assert "⚠" in missing.trailing_value(app)


@pytest.mark.asyncio
async def test_detail_strip_updates_on_cursor_move(built_index: Path) -> None:
    """Spec: Visual system › Detail strip — populates on focus change."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsList, SettingsScreen
    from fnd.tui.widgets import DetailStrip

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        strip = screen.query_one(DetailStrip)
        # Cursor at index 0 (Preferences). Strip shows Preferences description.
        # Gate on the strip's content rather than a fixed tick count: it fills
        # from a Highlighted message, whose round-trip can outlast a single
        # pause on a loaded runner (the strip then reads empty, not wrong).
        await wait_until(
            pilot,
            lambda: "preferences" in strip._description.lower(),
            message="DetailStrip never showed the Preferences description",
        )
        # Move cursor to Collections.
        lst = screen.query_one(SettingsList)
        lst.action_move(1)
        await wait_until(
            pilot,
            lambda: "collection" in strip._description.lower(),
            message="DetailStrip never updated to the Collections description",
        )


@pytest.mark.asyncio
async def test_hint_bar_appends_reveal_when_cursor_on_reveal_capable_row(
    built_index: Path,
) -> None:
    """Spec: Hint bar — append `Shift+⏎ Reveal` when row supports reveal."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        # Bridge focus from the filter Input → list so the hint cluster
        # reflects the row's affordances rather than the input cluster.
        await pilot.press("down")
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        idx = next(i for i, it in enumerate(lst._items) if it.id == "root.open_config_file")
        lst.cursor_index = idx
        await pilot.pause()
        # Assert on the resolved cluster (the actual logic) — Static.render
        # in the test pilot doesn't always reflect the last .update() call.
        cluster = screen._hint_cluster()
        labels = [label for _, label in cluster]
        assert "Reveal" in labels, (
            f"expected Reveal hint on Open config row; got cluster: {cluster!r}"
        )


@pytest.mark.asyncio
async def test_hint_bar_keybindings_variant(built_index: Path) -> None:
    """Spec: Hint bar — Keybindings screen shows `⏎ Run · [key] Run directly · Esc Back`."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one(SettingsList).focus()
        await pilot.pause()
        cluster = screen._hint_cluster()
        keys = [k for k, _ in cluster]
        labels = [lab for _, lab in cluster]
        assert "Run" in labels, f"expected Keybindings cluster; got: {cluster!r}"
        assert "[key]" in keys, f"expected press-key entry; got: {cluster!r}"


@pytest.mark.asyncio
async def test_hint_bar_search_focused_variant(built_index: Path) -> None:
    """Spec: Hint bar — Search input focused shows results / open-first / clear hints."""
    from textual.widgets import Input

    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one("#settings_search", Input).focus()
        await pilot.pause()
        cluster = screen._hint_cluster()
        labels = [label for _, label in cluster]
        assert "Clear" in labels or "Results" in labels, (
            f"expected search-focused cluster; got: {cluster!r}"
        )


@pytest.mark.asyncio
async def test_hint_bar_edit_bar_open_variant(built_index: Path) -> None:
    """Spec: Hint bar — Edit-bar open shows `⏎ Save · Esc Cancel`."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import EditBar, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Open Preferences and drill into a scalar to open the EditBar.
        from fnd.tui.menu import SECTION_PREFERENCES
        from fnd.tui.settings_screen import (
            SettingsList,
            open_settings_section,
        )

        open_settings_section(app, SECTION_PREFERENCES)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        # Find a KIND_SCALAR row.
        from fnd.tui.menu import KIND_SCALAR

        idx = next(i for i, it in enumerate(lst._items) if it.kind == KIND_SCALAR)
        lst.cursor_index = idx
        screen._activate_item(lst._items[idx])
        await pilot.pause()
        # EditBar should be open (no -hidden class).
        bar = screen.query_one(EditBar)
        assert "-hidden" not in bar.classes
        cluster = screen._hint_cluster()
        labels = [label for _, label in cluster]
        assert "Save" in labels, f"expected Save in edit-bar cluster; got: {cluster!r}"
        assert "Cancel" in labels, f"expected Cancel in edit-bar cluster; got: {cluster!r}"


@pytest.mark.asyncio
async def test_root_screen_shows_version_status_line(built_index: Path) -> None:
    """Spec: Use case A4 — version visible at the bottom of the root menu."""
    from textual.widgets import Static

    from fnd import __version__
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        status = screen.query_one("#settings_status", Static)
        rendered = str(status.render())
        assert __version__ in rendered, f"expected version in status; got: {rendered!r}"


@pytest.mark.asyncio
async def test_subscreen_omits_version_status(built_index: Path) -> None:
    """Sub-screens don't carry the version line — only the root does."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        # Keybindings sub-screen → no version status.
        assert screen._breadcrumb == ("Keybindings",)
        from textual.css.query import NoMatches

        try:
            screen.query_one("#settings_status")
            raise AssertionError("subscreen should not mount #settings_status")
        except NoMatches:
            pass


@pytest.mark.asyncio
async def test_preferences_refreshes_trailing_after_picker_pops(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: drilling into a KIND_PICKER setting (e.g. Drill row
    summaries) commits the new value via the picker, which pops. The
    parent SettingsScreen must re-render its trailings on resume —
    otherwise the user sees the old value until they leave and return."""
    from fnd.tui import FNDApp
    from fnd.tui.menu import SECTION_PREFERENCES
    from fnd.tui.settings_screen import (
        PickerScreen,
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PREFERENCES)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        drill_idx = next(i for i, it in enumerate(lst._items) if it.id == "pref.drill_summary_mode")
        lst.cursor_index = drill_idx
        # Before: trailing reads "always_show" (default).
        before = lst._items[drill_idx].trailing_value(app)
        assert "always_show" in before, before
        # Open picker.
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PickerScreen)
        # Manually commit a new value and pop, mirroring how a real user
        # toggles a radio in the picker.
        picker._commit({"always_ellipsis"})
        app.pop_screen()
        await pilot.pause()
        # After: the trailing must reflect the new mode without the user
        # leaving and returning.
        rendered_after = str(
            list(lst.query_one("#settings_list_body").children)[drill_idx].render()
        )
        assert "always_ellipsis" in rendered_after, (
            f"Trailing did not refresh after picker pop; got: {rendered_after!r}"
        )


@pytest.mark.asyncio
async def test_on_reindex_complete_swaps_searcher(fixtures_dir: Path, tmp_index_dir: Path) -> None:
    """Regression: after a reindex finishes the in-memory Searcher must
    be rebuilt — otherwise the captured ``self._index.searcher()`` keeps
    returning hits from the pre-rebuild generation and the user sees zero
    results until they restart the app."""
    from fnd.index import build_index
    from fnd.tui import FNDApp

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        original = app._search.searcher
        assert original is not None, "Searcher should exist after first mount"
        app._indexer.on_reindex_complete()
        assert app._search.searcher is not None
        assert app._search.searcher is not original, (
            "Searcher instance was not replaced — stale searcher would keep "
            "querying the pre-rebuild index generation"
        )


@pytest.mark.asyncio
async def test_on_screen_resume_rebuilds_items_from_provider(
    fixtures_dir: Path, tmp_index_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: when a structural edit lands in a popped child screen
    (here we simulate it by mutating the live Config and resuming), the
    parent SettingsScreen must re-walk its provider so newly-added rows
    appear. The earlier ``refresh_values()``-only path would silently
    drop the new row until the user fully reopened the screen."""
    from pathlib import Path as _Path

    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.index import build_index
    from fnd.tui import FNDApp
    from fnd.tui.menu import _provider_sources
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    cfg = Config(
        collections={
            "default": CollectionConfig(sources=[SourceConfig(path=fixtures_dir)]),
        }
    )
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Push a Sources sub-screen manually so we control the lifecycle.
        app.push_screen(
            SettingsScreen(
                breadcrumb=("Collections", "default", "Sources"),
                items=_provider_sources(app, "default"),
                provider=lambda a: tuple(_provider_sources(a, "default")),
            )
        )
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        before_ids = [it.id for it in lst._items]
        # Simulate a child screen mutating config: append a second source.
        cfg.collections["default"].sources.append(SourceConfig(path=_Path(str(tmp_path))))
        # Fire on_screen_resume directly — equivalent to a child popping.
        screen.on_screen_resume()
        await pilot.pause()
        after_ids = [it.id for it in lst._items]
        assert len(after_ids) > len(before_ids), (
            f"Item list did not grow after a structural edit. Before: "
            f"{before_ids!r}; After: {after_ids!r}"
        )


@pytest.mark.asyncio
async def test_picker_toggle_preserves_cursor(fixtures_dir: Path, tmp_index_dir: Path) -> None:
    """Regression: toggling a multi-select option (file type, excludes
    preset, etc.) must keep the OptionList cursor on the just-toggled
    row. The previous implementation called ``clear_options() +
    rebuild`` which reset the highlight to 0 every time."""
    from textual.widgets import OptionList

    from fnd.index import build_index
    from fnd.tui import FNDApp
    from fnd.tui.menu import (
        KIND_PICKER,
        ChoiceOption,
        MenuItem,
    )
    from fnd.tui.settings_screen import PickerScreen

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    selected: set[str] = set()
    item = MenuItem(
        id="x.multi",
        label="Multi",
        kind=KIND_PICKER,
        multi=True,
        choices_provider=lambda _a: [
            ChoiceOption(value="a", label="Alpha"),
            ChoiceOption(value="b", label="Bravo"),
            ChoiceOption(value="c", label="Charlie"),
        ],
        picker_getter=lambda _a: sorted(selected),
        picker_setter=lambda _a, v: selected.update(v) or None,  # type: ignore[func-returns-value]
    )
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(PickerScreen(item))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickerScreen)
        lst = screen.query_one("#picker_list", OptionList)
        # Move cursor to "Bravo" (index 1) and toggle it.
        lst.highlighted = 1
        await pilot.press("enter")
        await pilot.pause()
        assert "b" in screen._selected, "expected the picker to register the toggle"
        assert lst.highlighted == 1, (
            f"cursor jumped to {lst.highlighted!r} after toggle; should stay at 1"
        )


@pytest.mark.asyncio
async def test_collections_sidebar_toggle_preserves_cursor(
    fixtures_dir: Path, tmp_index_dir: Path, tmp_path: Path, saved_empty_scope: Path
) -> None:
    """Regression: toggling a collection in the main app's sidebar tree
    must keep the cursor on the toggled row. The previous implementation
    called ``_refresh_collections_panel()`` which does ``tree.clear()``
    and resets the cursor to the root every time."""
    from textual.widgets import Tree

    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.index import build_index
    from fnd.tui import FNDApp

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    cfg = Config(
        collections={
            "alpha": CollectionConfig(sources=[SourceConfig(path=fixtures_dir)]),
            "bravo": CollectionConfig(sources=[SourceConfig(path=fixtures_dir)]),
        }
    )
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#collections_panel_tree", Tree)
        # Move cursor to the second collection node (bravo).
        bravo = tree.root.children[1]
        tree.cursor_line = bravo.line
        await pilot.pause()
        cursor_before = tree.cursor_line
        assert cursor_before > 0, "expected cursor to land on a non-root row"
        # Fire the selection directly — Tree.action_select_cursor in
        # headless tests doesn't always post NodeSelected via the focus
        # path. Posting the message exercises the real handler.
        tree.post_message(Tree.NodeSelected(bravo))
        await pilot.pause()
        assert "bravo" in app._scope.collections, "toggle did not register"
        assert tree.cursor_line == cursor_before, (
            f"cursor moved from {cursor_before} to {tree.cursor_line} after a single toggle"
        )
        # Label marker should now read ● (active) for bravo.
        label_str = str(bravo.label)
        assert label_str.startswith("●"), f"expected ● marker after toggle; got {label_str!r}"


@pytest.mark.asyncio
async def test_cursor_move_does_not_call_render_all(
    fixtures_dir: Path, tmp_index_dir: Path
) -> None:
    """Regression / perf: arrow-key cursor movement must NOT call
    SettingsList._render_all — that path rebuilds every row's Rich
    Text and was the dominant per-keystroke cost on long lists like
    Keybindings."""
    from fnd.index import build_index
    from fnd.tui import FNDApp
    from fnd.tui.menu import SECTION_KEYBINDINGS
    from fnd.tui.settings_screen import (
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_KEYBINDINGS)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        counter = {"n": 0}
        orig = lst._render_all

        def _counting_render() -> None:
            counter["n"] += 1
            orig()

        # Track calls to _render_all across a cursor walk.
        lst._render_all = _counting_render  # type: ignore[method-assign]
        baseline = counter["n"]
        for _ in range(10):
            lst.cursor_index += 1
        await pilot.pause()
        assert counter["n"] == baseline, (
            f"watch_cursor_index called _render_all "
            f"{counter['n'] - baseline} times across 10 cursor moves; "
            "should be zero — only -cursor class toggles, no row rerenders"
        )


class TestSourceRowMatchesTheWalk:
    """A row that names file types must not contradict what is indexed."""

    @staticmethod
    def _row(tmp_path, includes):  # type: ignore[no-untyped-def]
        from fnd.config import Config
        from fnd.tui.menu import _source_trailing

        cfg = Config.model_validate(
            {
                "defaults": {"filters": {"exclude_tags": []}},
                "collections": {"c": {"sources": [{"path": str(tmp_path), "includes": includes}]}},
            }
        )

        class _App:
            _config = cfg

        return _source_trailing("c", 0)(_App())  # type: ignore[arg-type]

    def test_a_path_glob_means_the_types_are_not_restricted(self, tmp_path: Path) -> None:
        """Include globs are ORed, so ``notes/**`` admits every type under
        notes/ whatever its neighbours say. Reading the suffixes alone said
        "md" for a source that yields PDFs."""
        assert "All types" in self._row(tmp_path, ["**/*.md", "notes/**"])
        assert "path globs" in self._row(tmp_path, ["**/*.md", "notes/**"])

    def test_a_suffix_glob_still_names_its_type(self, tmp_path: Path) -> None:
        assert self._row(tmp_path, ["**/*.md"]) == "md"

    def test_no_includes_is_every_type(self, tmp_path: Path) -> None:
        assert self._row(tmp_path, []) == "All types"
