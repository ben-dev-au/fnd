"""Toggling one source's checkbox deleted a source added to the file by hand.

`action_save_close` builds its write from `app._config`, loaded at launch, and
`write_collection` replaces the collection table wholesale, so anything added
to that table after launch is written away. Scalar hand-edits survive because
they live in `[defaults.filters]`, which this write does not touch.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from fnd.config import load
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SourceFormScreen


@pytest.fixture
def two_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("one", "two", "three"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.md").write_text("saffron\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.vault.sources]]
            path = "{(tmp_path / "one").as_posix()}"

            [[collections.vault.sources]]
            path = "{(tmp_path / "two").as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return cfg_path


def _append_by_hand(cfg_path: Path, folder: Path) -> None:
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8")
        + f'\n[[collections.vault.sources]]\npath = "{folder.as_posix()}"\n',
        encoding="utf-8",
    )


async def _open_form(app: FNDApp, pilot: Any, index: int) -> SourceFormScreen:
    app._config = load()
    screen = SourceFormScreen(collection_name="vault", source_index=index)
    app.push_screen(screen)
    for _ in range(20):
        await pilot.pause()
    return screen


@pytest.mark.asyncio
async def test_a_hand_added_source_survives_an_unrelated_save(
    two_sources: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _open_form(app, pilot, 0)

        _append_by_hand(two_sources, tmp_path / "three")
        screen._fields["follow_symlinks"] = True
        screen.action_save_close()
        for _ in range(20):
            await pilot.pause()

    after = load(two_sources)
    paths = [str(s.path) for s in after.collections["vault"].sources]
    assert len(paths) == 3, f"the hand-added source was written away: {paths}"
    assert any("three" in p for p in paths), paths


@pytest.mark.asyncio
async def test_the_edit_still_lands(two_sources: Path, tmp_path: Path, tmp_index_dir: Path) -> None:
    """The control: reloading must not cost the user their edit."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _open_form(app, pilot, 0)
        screen._fields["follow_symlinks"] = True
        screen.action_save_close()
        for _ in range(20):
            await pilot.pause()

    after = load(two_sources)
    assert after.collections["vault"].sources[0].follow_symlinks is True


@pytest.mark.asyncio
async def test_a_reordered_file_is_refused_rather_than_guessed(
    two_sources: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A fresh read makes the row index mean something else. Refuse, do not
    write the edit onto whichever source now sits at that position."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _open_form(app, pilot, 0)

        two_sources.write_text(
            textwrap.dedent(f"""
                [[collections.vault.sources]]
                path = "{(tmp_path / "two").as_posix()}"

                [[collections.vault.sources]]
                path = "{(tmp_path / "one").as_posix()}"
            """),
            encoding="utf-8",
        )
        before = two_sources.read_text(encoding="utf-8")
        screen._fields["follow_symlinks"] = True
        screen.action_save_close()
        for _ in range(20):
            await pilot.pause()

    assert two_sources.read_text(encoding="utf-8") == before, "it wrote onto the wrong source"


@pytest.mark.asyncio
async def test_a_config_that_will_not_load_refuses_rather_than_falling_back(
    two_sources: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The fallback made the guard vacuous and did the write it exists to stop.

    `load()` raising sent it back to `app._config` (the very model
    `_snapshot` came from), so the identity check could never disagree, and the
    stale write went ahead. A validation failure, not just malformed TOML.
    """
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _open_form(app, pilot, 0)

        two_sources.write_text(
            two_sources.read_text(encoding="utf-8")
            + f'\n[[collections.vault.sources]]\npath = "{(tmp_path / "three").as_posix()}"\n'
            + 'follow_symlinks = "maybe"\n',
            encoding="utf-8",
        )
        before = two_sources.read_text(encoding="utf-8")
        screen._fields["follow_symlinks"] = True
        screen.action_save_close()
        for _ in range(20):
            await pilot.pause()

    assert two_sources.read_text(encoding="utf-8") == before, "it wrote from the stale model"


@pytest.mark.asyncio
async def test_an_empty_snapshot_does_not_wave_the_guard_through(
    two_sources: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """`not opened` short-circuited the identity check to True."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _open_form(app, pilot, 0)
        screen._snapshot = {}

        two_sources.write_text(
            textwrap.dedent(f"""
                [[collections.vault.sources]]
                path = "{(tmp_path / "two").as_posix()}"

                [[collections.vault.sources]]
                path = "{(tmp_path / "one").as_posix()}"
            """),
            encoding="utf-8",
        )
        before = two_sources.read_text(encoding="utf-8")
        screen._fields["follow_symlinks"] = True
        screen.action_save_close()
        for _ in range(20):
            await pilot.pause()

    assert two_sources.read_text(encoding="utf-8") == before, "it wrote onto the wrong source"


@pytest.mark.asyncio
async def test_the_refusal_advice_actually_works(
    two_sources: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """ "Press Esc and reopen" re-read the same stale model and refused again.

    The refusal returned before `app._config = cfg`, and `_load_snapshot` reads
    `app._config`, so the reopened form was seeded from the model that was
    already wrong, and the loop only broke on restart.
    """
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _open_form(app, pilot, 0)

        two_sources.write_text(
            textwrap.dedent(f"""
                [[collections.vault.sources]]
                path = "{(tmp_path / "two").as_posix()}"

                [[collections.vault.sources]]
                path = "{(tmp_path / "one").as_posix()}"
            """),
            encoding="utf-8",
        )
        screen._fields["follow_symlinks"] = True
        screen.action_save_close()
        for _ in range(15):
            await pilot.pause()

        # Take the advice: leave and open the same row again.
        app.pop_screen()
        for _ in range(10):
            await pilot.pause()
        reopened = SourceFormScreen(collection_name="vault", source_index=0)
        app.push_screen(reopened)
        for _ in range(20):
            await pilot.pause()
        seeded = str(reopened._snapshot.get("path") or "")

    assert "two" in seeded, f"the reopened form was seeded from the stale model: {seeded}"
