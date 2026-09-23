"""`Down` from the safe row of an irreversible dialog does not wrap onto "Yes".

Cancel sits LAST, and a two-item OptionList wraps, so `Down` (which on every
other list in the app moves within the list) would land on "Yes, delete…"; one
slip drops and rebuilds a renamed collection unasked.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text
from textual.widgets import OptionList

from fnd.config import Config, load
from fnd.tui import FNDApp
from fnd.tui.settings_screen import (
    CacheMaintenanceConfirm,
    DeleteCollectionScreen,
    DeleteSourceScreen,
    UpdateAllConfirm,
)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("saffron\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.papers.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


async def _after(app: FNDApp, pilot: Any, screen: Any, *keys: str) -> str | None:
    app.push_screen(screen)
    for _ in range(15):
        await pilot.pause()
    for key in keys:
        await pilot.press(key)
        await pilot.pause()
    options = app.screen.query_one("#confirm_list", OptionList)
    return options._options[options.highlighted or 0].id


def _screens() -> list[tuple[str, Any]]:
    return [
        ("delete collection", lambda: DeleteCollectionScreen(collection_name="papers")),
        ("delete source", lambda: DeleteSourceScreen(collection_name="papers", source_index=0)),
        (
            "clear cache",
            lambda: CacheMaintenanceConfirm(
                title="Clear texture cache",
                summary=Text("Deletes every saved texturing."),
                run=lambda: 0,
                confirm_label="Yes, clear it",
                result_label="cleared",
                irreversible=True,
            ),
        ),
    ]


@pytest.mark.parametrize(("name", "make"), _screens())
@pytest.mark.asyncio
async def test_down_from_the_safe_row_does_not_wrap_onto_yes(
    name: str, make: Any, config: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        landed = await _after(app, pilot, make(), "down")

    assert landed != "yes", f"{name}: one Down armed the irreversible row"


@pytest.mark.parametrize(("name", "make"), _screens())
@pytest.mark.asyncio
async def test_up_still_reaches_it(
    name: str, make: Any, config: Config, tmp_index_dir: Path
) -> None:
    """The control: the affirmative must stay reachable, deliberately."""
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        landed = await _after(app, pilot, make(), "up")

    assert landed == "yes", f"{name}: Up no longer reaches the affirmative"


@pytest.mark.asyncio
async def test_a_safe_dialog_still_moves_both_ways(config: Config, tmp_index_dir: Path) -> None:
    """Update all starts on Yes, so Down must still reach Cancel."""
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        landed = await _after(app, pilot, UpdateAllConfirm(collection_names=["papers"]), "down")

    assert landed == "no", landed


def test_every_confirm_list_is_the_non_wrapping_one() -> None:
    """A new confirm screen must not reintroduce the wrap quietly."""
    import ast

    source = Path("fnd/tui/settings_screen.py").read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if '"confirm_list"' not in segment:
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "ConfirmList":
            offenders.append(segment.splitlines()[0])
    assert not offenders, f"confirm lists that still wrap: {offenders}"
