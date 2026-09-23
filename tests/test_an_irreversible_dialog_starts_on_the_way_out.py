"""A confirm dialog whose Yes cannot be undone starts on Cancel.

Every one of these screens is reached by pressing Enter, so landing the cursor
on "Yes, delete…" puts the irreversible act one keypress from the one that
opened it. The rebuild, rename and delete screens and the cache clear share the
same hand-rolled `on_mount`.

`UpdateAllConfirm` is the control: its affirmative is safe, and nothing moves.

The footer follows the cursor: `⏎ Confirm` while Enter cancels is silent, so the
screen you land on is indistinguishable from the one you would land on if it
had worked.
"""

from __future__ import annotations

import ast
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


async def _landed_on(app: FNDApp, pilot: Any, screen: Any) -> str | None:
    app.push_screen(screen)
    for _ in range(15):
        await pilot.pause()
    options = app.screen.query_one("#confirm_list", OptionList)
    return options._options[options.highlighted or 0].id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make",
    [
        lambda: DeleteCollectionScreen(collection_name="papers"),
        lambda: DeleteSourceScreen(collection_name="papers", source_index=0),
        lambda: CacheMaintenanceConfirm(
            title="Clear texture cache",
            summary=Text("Deletes every saved texturing."),
            run=lambda: 0,
            confirm_label="Yes, clear it",
            result_label="cleared",
            irreversible=True,
        ),
    ],
    ids=["delete-collection", "delete-source", "clear-cache"],
)
async def test_it_starts_on_cancel(config: Config, tmp_index_dir: Path, make: Any) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        landed = await _landed_on(app, pilot, make())

    assert landed == "no", "Enter on arrival would have done it"


@pytest.mark.asyncio
async def test_a_recoverable_act_is_not_moved(config: Config, tmp_index_dir: Path) -> None:
    """The control: the rule is irreversibility, not confirm screens at large."""
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        landed = await _landed_on(app, pilot, UpdateAllConfirm(collection_names=["papers"]))

    assert landed == "yes"


@pytest.mark.asyncio
async def test_a_reversible_cache_run_is_not_moved(config: Config, tmp_index_dir: Path) -> None:
    """The same screen serves both, so the flag is what decides."""
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        landed = await _landed_on(
            app,
            pilot,
            CacheMaintenanceConfirm(
                title="Prune orphaned texturings",
                summary=Text("Drops entries for files no longer on disk."),
                run=lambda: 0,
                confirm_label="Yes, prune them",
                result_label="pruned",
            ),
        )

    assert landed == "yes"


def test_no_confirm_screen_focuses_the_list_itself() -> None:
    """Seven screens hand-rolled this; two of them got it right. One helper
    means the next one cannot get it wrong quietly."""
    source = Path("fnd/tui/settings_screen.py").read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for f in node.body:
            if not isinstance(f, ast.FunctionDef) or f.name != "on_mount":
                continue
            body = ast.get_source_segment(source, f) or ""
            if "#confirm_list" not in body:
                continue
            if "open_confirm_list" not in body:
                offenders.append(node.name)
    assert not offenders, f"confirm screens that place their own cursor: {offenders}"


async def _footer_and_cursor(app: FNDApp, pilot: Any, screen: Any) -> tuple[str, str | None]:
    app.push_screen(screen)
    for _ in range(15):
        await pilot.pause()
    options = app.screen.query_one("#confirm_list", OptionList)
    landed = options._options[options.highlighted or 0].id
    footer = "".join(s.text for s in app.screen._compositor.render_strips()[-1])
    return footer, landed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make",
    [
        lambda: DeleteCollectionScreen(collection_name="papers"),
        lambda: DeleteSourceScreen(collection_name="papers", source_index=0),
        lambda: CacheMaintenanceConfirm(
            title="Clear texture cache",
            summary=Text("Deletes every saved texturing."),
            run=lambda: 0,
            confirm_label="Yes, clear it",
            result_label="cleared",
            irreversible=True,
        ),
        lambda: UpdateAllConfirm(collection_names=["papers"]),
    ],
    ids=["delete-collection", "delete-source", "clear-cache", "update-all"],
)
async def test_the_footer_says_what_enter_will_do(
    config: Config, tmp_index_dir: Path, make: Any
) -> None:
    """The invariant, across every confirm screen: the footer never promises
    `Confirm`.

    The hint is computed only on arrival, so even where Enter confirms then,
    one `Down` leaves the promise standing while Enter cancels. A screen that
    cannot recompute the hint cannot make the promise.
    """
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        footer, _landed = await _footer_and_cursor(app, pilot, make())

    assert "Confirm" not in footer, footer
    assert "Select" in footer, footer
