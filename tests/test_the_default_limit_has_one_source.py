"""Every fallback for `result_limit` reads the model default, not a copy of it.

The defect is a second number: `search_controller` holding `limit=50` while
`defaults.result_limit` says otherwise. Three call sites each carrying their
own literal is the same hazard one drift away.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import DEFAULT_RESULT_LIMIT, Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


def _corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int) -> Config:
    """A config that sets no `result_limit`, so the fallback is what runs."""
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(n):
        (root / f"note{i:03d}.md").write_text(
            f"# Note {i}\n\nsaffron appears here.\n", encoding="utf-8"
        )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.mark.asyncio
async def test_an_unset_limit_falls_back_to_the_model_default(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the setting absent, the searcher stops at the declared default."""
    cfg = _corpus(tmp_path, monkeypatch, DEFAULT_RESULT_LIMIT + 10)
    build_index(roots=[tmp_path / "notes"], index_dir=tmp_index_dir, collection="notes")
    app = FNDApp(index_dir=tmp_index_dir, config=cfg, collection="notes", initial_query="saffron")

    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(40):
            await pilot.pause()
        found = len(app._search.groups)

    assert found == DEFAULT_RESULT_LIMIT, found


def test_the_preferences_row_shows_the_model_default() -> None:
    """The Preferences row and the model cannot disagree about the default."""
    from types import SimpleNamespace

    from fnd.config import Defaults
    from fnd.tui.menu import _provider_preferences

    unconfigured = SimpleNamespace(_config=None)
    row = next(
        i
        for i in _provider_preferences(unconfigured)  # type: ignore[arg-type]
        if i.id == "pref.result_limit"
    )
    assert row.value_getter is not None
    shown = row.value_getter(unconfigured)  # type: ignore[arg-type]

    assert shown == str(Defaults().result_limit) == str(DEFAULT_RESULT_LIMIT), shown
