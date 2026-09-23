"""An editor holding old values cannot write them over a newer save.

`:` (the screen's own "Menu" key) can open the palette OVER the Index filters
editor, the palette can walk into a SECOND Index filters, and `^s` on the first
must not revert the second's save: `modified_after = 2026-08-09` must not
become `# modified_after =` again.

Re-reading the file at write time does not help: the VALUES being written are
the stale ones. The only thing that catches it is noticing the file moved.

The CLI reaches the same place: write a source from the command line while a
form is open, save the form, and the source must survive.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import ConfigChangedError, config_fingerprint, write_settings


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"
        """),
        encoding="utf-8",
    )
    return path


def test_a_fingerprint_changes_when_the_file_does(config_file: Path) -> None:
    before = config_fingerprint(config_file)
    write_settings(config_path=config_file, values={"defaults.result_limit": 7})

    assert config_fingerprint(config_file) != before


def test_it_is_stable_when_nothing_changes(config_file: Path) -> None:
    """The control: a guard that fires on an unchanged file refuses every
    save, which is its own way of losing work."""
    assert config_fingerprint(config_file) == config_fingerprint(config_file)


def test_two_saves_a_keystroke_apart_are_told_apart(config_file: Path) -> None:
    """Bytes, not mtime. A second-granularity clock cannot separate two saves
    in the same second, which is exactly the case that loses work."""
    write_settings(config_path=config_file, values={"defaults.result_limit": 7})
    first = config_fingerprint(config_file)
    write_settings(config_path=config_file, values={"defaults.result_limit": 8})

    assert config_fingerprint(config_file) != first


def test_a_missing_file_fingerprints_to_nothing(tmp_path: Path) -> None:
    assert config_fingerprint(tmp_path / "absent.toml") == ""


@pytest.mark.asyncio
async def test_the_editor_refuses_rather_than_reverting(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real save closure: open the browser, let the
    file change underneath, then save."""
    from fnd.config import load
    from fnd.tui import FNDApp
    from fnd.tui.menu import _open_filter_browser
    from fnd.tui.settings_screen import FilterBrowserScreen

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    app = FNDApp(index_dir=tmp_index_dir, config=load(cfg_path))
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        _open_filter_browser(app)
        for _ in range(25):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FilterBrowserScreen), screen
        # Someone else saves: a second editor, or the CLI.
        write_settings(config_path=cfg_path, values={"defaults.result_limit": 7})
        after_theirs = cfg_path.read_text(encoding="utf-8")

        with pytest.raises(ConfigChangedError):
            screen._on_save(screen._spec, screen._gitignore, screen._fndignore)

    assert cfg_path.read_text(encoding="utf-8") == after_theirs, "it wrote anyway"
