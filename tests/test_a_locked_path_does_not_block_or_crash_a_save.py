"""The same non-total `exists()`, at three more sites on this branch.

`save_blocked` is called unguarded from `action_save_close` on both forms, so
a path under a directory that denies search took the app down on `^s` instead
of showing its error label. The leave path swallows the same exception
(`save_blocked_on` catches everything), so Esc was fine and only saving
crashed, and the prompt went on offering a save that would kill the app.

`EditBar._validate_path` runs the same call from a debounce timer on those two
fields, so it fires 250 ms into typing, before `^s` is reached at all.
"""

from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fnd.tui.settings_screen import AddCollectionWizard, EditBar, SourceFormScreen


def _locked(tmp_path: Path) -> Path:
    inner = tmp_path / "Volume" / "Notes"
    inner.mkdir(parents=True)
    os.chmod(tmp_path / "Volume", 0o000)
    if os.access(tmp_path / "Volume", os.R_OK):
        os.chmod(tmp_path / "Volume", stat_mod.S_IRWXU)
        pytest.skip("running as a user that bypasses directory permissions")
    return inner


def _form(cls: type, path: str) -> Any:
    """The method under test reads `_fields` and `app._config`, so give it
    those and no UI. `app` is a property, hence the subclass."""
    probe = type(f"_{cls.__name__}Probe", (cls,), {"app": SimpleNamespace(_config=None)})
    screen = object.__new__(probe)
    screen._fields = {"path": path, "name": "newcoll"}
    return screen


@pytest.mark.parametrize("cls", [SourceFormScreen, AddCollectionWizard])
def test_a_path_we_cannot_reach_does_not_block_the_save(cls: type, tmp_path: Path) -> None:
    inner = _locked(tmp_path)
    try:
        answer = _form(cls, str(inner)).save_blocked()
    finally:
        os.chmod(tmp_path / "Volume", stat_mod.S_IRWXU)

    assert answer == "", answer


@pytest.mark.parametrize("cls", [SourceFormScreen, AddCollectionWizard])
def test_a_path_that_is_really_gone_still_blocks(cls: type, tmp_path: Path) -> None:
    """The control: the check still has to be able to refuse."""
    answer = _form(cls, str(tmp_path / "never")).save_blocked()

    assert "does not exist" in answer, answer


def test_the_edit_bar_says_unreadable_rather_than_gone(tmp_path: Path) -> None:
    """A timer callback that raises takes the whole app down, and this one runs
    250 ms into typing. `⚠ unreadable` is a state this bar already has."""
    inner = _locked(tmp_path)
    said: list[str] = []
    bar = object.__new__(EditBar)
    bar._validation_timer = None
    bar._set_status = lambda text, tone="ok": said.append(str(text))  # type: ignore[method-assign]

    try:
        bar._validate_path(str(inner))
    finally:
        os.chmod(tmp_path / "Volume", stat_mod.S_IRWXU)

    assert said, "the bar said nothing at all"
    assert "does not exist" not in said[-1], said[-1]
    assert "unreadable" in said[-1], said[-1]


def test_the_sources_row_does_not_call_an_unreadable_path_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`⚠ path not found` for a folder that is there is the same wrong answer
    the batch removed from the prune guard and from the open action."""
    from types import SimpleNamespace

    from fnd.config import CollectionConfig, SourceConfig
    from fnd.tui.menu import _source_trailing

    inner = _locked(tmp_path)
    cfg = SimpleNamespace(collections={"c": CollectionConfig(sources=[SourceConfig(path=inner)])})
    app = SimpleNamespace(_config=cfg)

    try:
        summary = _source_trailing("c", 0)(app)  # type: ignore[arg-type]
    finally:
        os.chmod(tmp_path / "Volume", stat_mod.S_IRWXU)

    assert "path not found" not in summary, summary
