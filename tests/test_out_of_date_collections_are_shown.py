"""An index that no longer matches its config is shown, never silently fixed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.index_freshness import SIDECAR_NAME, Ledger, State, Verdict, indexed_with
from fnd.tui import FNDApp
from fnd.tui.freshness_view import MARKER, sidebar_value, verdict_for


def _cfg(tmp_path: Path, **source: Any) -> Config:
    notes = tmp_path / "notes"
    notes.mkdir(exist_ok=True)
    return Config(
        collections={"notes": CollectionConfig(sources=[SourceConfig(path=notes, **source)])}
    )


def _record_clean(tmp_path: Path, index_dir: Path) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    clean = _cfg(tmp_path)
    Ledger(index_dir).record("notes", indexed_with(clean.collections["notes"], clean.defaults))


def _row(app: FNDApp) -> str:
    return str(app.query_one("#collections_panel_tree").root.children[0].label)  # type: ignore[attr-defined]


async def _settle(pilot: Any) -> None:
    for _ in range(5):
        await pilot.pause()


def test_the_sidebar_value_names_the_remedy() -> None:
    """Each state reads as the work it asks for, in both widths."""
    assert sidebar_value(Verdict(State.CURRENT), 3) == ("3 sources", "3 src")
    assert sidebar_value(Verdict(State.NEEDS_UPDATE), 3) == (
        f"3 sources · {MARKER} outdated",
        f"3 src {MARKER}",
    )
    assert sidebar_value(Verdict(State.NEEDS_REBUILD), 1) == (
        f"1 source · {MARKER} tags outdated",
        f"1 src {MARKER}",
    )
    assert sidebar_value(Verdict(State.NOT_INDEXED), 1) == ("not indexed", "new")


def test_the_marker_is_one_cell() -> None:
    """The row budget counts cells; a wide glyph would push the name off."""
    from rich.cells import cell_len

    assert cell_len(MARKER) == 1


@pytest.mark.asyncio
async def test_a_changed_source_marks_its_sidebar_row(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The row says outdated when the config moved past the recorded run."""
    _record_clean(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=_cfg(tmp_path, excludes=["build/**"]))
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        assert verdict_for(app, "notes").state is State.NEEDS_UPDATE
        assert MARKER in _row(app)


@pytest.mark.asyncio
async def test_a_current_collection_carries_no_marker(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The control: nothing changed, nothing shown."""
    _record_clean(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=_cfg(tmp_path))
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        assert MARKER not in _row(app)
        assert "not indexed" not in _row(app)


@pytest.mark.asyncio
async def test_an_index_from_before_the_sidecar_is_adopted(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Collections already holding documents must not all read not indexed."""
    from fnd.index import build_index

    cfg = _cfg(tmp_path)
    (tmp_path / "notes" / "a.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    build_index(roots=[tmp_path / "notes"], index_dir=tmp_index_dir, collection="notes")
    # An index from before this feature has no record at all.
    (tmp_index_dir / SIDECAR_NAME).unlink()
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 30)) as pilot:
        for _ in range(40):
            await pilot.pause()
            if Ledger(tmp_index_dir).recorded("notes") is not None:
                break
        await _settle(pilot)
        assert "not indexed" not in _row(app)
        assert verdict_for(app, "notes").state is State.CURRENT


@pytest.mark.asyncio
async def test_u_in_the_collections_panel_runs_the_update(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`u` on an out-of-date row starts the Update it names."""
    _record_clean(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=_cfg(tmp_path, excludes=["build/**"]))
    started: list[tuple[str, bool]] = []
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        monkeypatch.setattr(
            app._indexer,
            "reindex_with_warning",
            lambda name, **kw: started.append((name, bool(kw.get("rebuild", False)))),
        )
        app.query_one("#collections_panel_tree").focus()
        await pilot.press("u")
        await _settle(pilot)
    assert started == [("notes", False)]


@pytest.mark.asyncio
async def test_a_rebuild_is_confirmed_before_it_runs(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Re-reading every file is costly, so `u` asks, landing on the safe row."""
    from fnd.tui.settings_screen import RebuildConfirmScreen

    _record_clean(tmp_path, tmp_index_dir)
    cfg = _cfg(tmp_path)
    cfg.defaults.tag_frontmatter_keys = ["Course"]
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        app.query_one("#collections_panel_tree").focus()
        await pilot.press("u")
        await _settle(pilot)
        assert isinstance(app.screen, RebuildConfirmScreen)
        assert app.screen.query_one("#confirm_list").highlighted == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_u_on_the_collection_page_runs_the_update(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The toast names `u`, so it works on the Settings page the save lands on."""
    from fnd.tui.menu import _make_open_collection_screen

    _record_clean(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=_cfg(tmp_path, excludes=["build/**"]))
    started: list[str] = []
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        monkeypatch.setattr(
            app._indexer, "reindex_with_warning", lambda name, **kw: started.append(name)
        )
        _make_open_collection_screen("notes")(app)
        await _settle(pilot)
        from fnd.tui.settings_screen import SettingsList

        app.screen.query_one(SettingsList).focus()
        await pilot.press("u")
        await _settle(pilot)
    assert started == ["notes"]


@pytest.mark.asyncio
async def test_the_update_row_says_why_it_is_needed(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The collection page names the remedy on the row that runs it."""
    from fnd.tui.menu import _summary_collection_update

    _record_clean(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=_cfg(tmp_path, excludes=["build/**"]))
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        assert _summary_collection_update(app, "notes").startswith("Outdated")


@pytest.mark.asyncio
async def test_a_settings_change_redraws_the_sidebar_marker(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tag key set in Settings marks the row at once, not after a restart."""
    from fnd.config import load, write_collection

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    (tmp_path / "notes").mkdir()
    write_collection(
        config_path=cfg_path,
        name="notes",
        collection=CollectionConfig(sources=[SourceConfig(path=tmp_path / "notes")]),
    )
    cfg = load(cfg_path)
    tmp_index_dir.mkdir(parents=True, exist_ok=True)
    Ledger(tmp_index_dir).record("notes", indexed_with(cfg.collections["notes"], cfg.defaults))
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        from fnd.tui.menu import walk_all_sections

        item = next(i for _c, i in walk_all_sections(app) if i.id == "filters.tag_frontmatter_keys")
        from fnd.tui.settings_screen import EditBar, SettingsScreen

        screen = SettingsScreen(breadcrumb=("Filters",), items=(item,))
        app.push_screen(screen)
        await _settle(pilot)
        screen.post_message(EditBar.EditCommitted(item, ["Course"]))
        await _settle(pilot)
        assert MARKER in _row(app)


@pytest.mark.asyncio
async def test_the_update_row_is_labelled_needed_with_its_reason(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The row that runs the remedy says it is needed, and why, where the user reads it."""
    from fnd.tui.menu import _provider_collection

    _record_clean(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=_cfg(tmp_path, excludes=["build/**"]))
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        row = next(i for i in _provider_collection(app, "notes") if i.id == "col.notes.reindex")
    assert row.label == "Update index"
    assert row.description.startswith("Outdated: Excludes changed since the last index. ")
    from fnd.tui.freshness_view import BADGE_STYLE
    from fnd.tui.settings_screen import _trailing_segments

    assert _trailing_segments(row, app)[0] == ("Outdated", BADGE_STYLE)


@pytest.mark.asyncio
async def test_a_current_update_row_reads_as_before(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The control: nothing needed, nothing added."""
    from fnd.tui.menu import _provider_collection

    _record_clean(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=_cfg(tmp_path))
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        row = next(i for i in _provider_collection(app, "notes") if i.id == "col.notes.reindex")
    assert row.label == "Update index"
    assert row.description.startswith("Add new")
    from fnd.tui.settings_screen import _trailing_segments

    assert _trailing_segments(row, app) == [("[ Update ]", "bold cyan")]


@pytest.mark.asyncio
async def test_u_waits_for_a_renames_drop(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index takes one writer: no run starts, from any row, while a rename drops."""
    import threading

    from fnd.tui.freshness_view import run_pending

    _record_clean(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=_cfg(tmp_path, excludes=["build/**"]))
    started: list[str] = []
    seen: list[str] = []
    release = threading.Event()
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(pilot)
        monkeypatch.setattr(
            app._indexer, "start", lambda collection, **_k: started.append(collection)
        )
        monkeypatch.setattr(app, "notify", lambda m, *a, **k: seen.append(str(m)))
        app.run_worker(lambda: release.wait(5), thread=True, group="rename-old")
        await _settle(pilot)
        app._indexer.reindex_with_warning("notes")
        run_pending(app, "notes")
        release.set()
        await app.workers.wait_for_complete()
    assert started == []
    assert any("still being dropped" in m for m in seen), seen


def test_a_mixed_save_toast_names_each_collection_and_its_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Update all fixes only outdated ones, so a mixed set is named one by one."""
    from types import SimpleNamespace

    from fnd.tui.freshness_view import announce_saved

    verdicts = {
        "notes": Verdict(State.NEEDS_UPDATE, ("Excludes",)),
        "papers": Verdict(State.NEEDS_REBUILD, ("Tag sources",)),
    }
    monkeypatch.setattr("fnd.tui.freshness_view.verdict_for", lambda _a, n: verdicts[n])
    seen: list[str] = []
    app = SimpleNamespace(notify=lambda m, **_k: seen.append(m))
    announce_saved(app, ["notes", "papers"])  # type: ignore[arg-type]
    assert seen == [
        "Saved. 'notes' is outdated and 'papers' has outdated tags: "
        "press u on each in Collections to bring it up to date."
    ]


def test_an_all_outdated_save_toast_points_at_update_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: when Update all fixes every one, it is the one action named."""
    from types import SimpleNamespace

    from fnd.tui.freshness_view import announce_saved

    monkeypatch.setattr(
        "fnd.tui.freshness_view.verdict_for",
        lambda _a, _n: Verdict(State.NEEDS_UPDATE, ("Index filters",)),
    )
    seen: list[str] = []
    announce_saved(SimpleNamespace(notify=lambda m, **_k: seen.append(m)), ["a", "b"])  # type: ignore[arg-type]
    assert seen == [
        "Saved. 2 collections are outdated: "
        "Settings › Collections › Update all collections brings them up to date."
    ]
