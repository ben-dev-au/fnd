"""A screen that lists settings rows shows the focused row's description.

Every MenuItem already carries one. SettingsScreen and AddCollectionWizard
each render it in a DetailStrip (the wizard's handler says "Mirror the
SettingsScreen pattern"), and SourceFormScreen, which edits the same fields
and had eighteen blank lines to spare, showed nothing. The explainer is the
house strategy; it was applied to two screens of three.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SourceFormScreen

_MODULE = Path(__file__).resolve().parent.parent / "fnd" / "tui" / "settings_screen.py"


def test_every_screen_with_a_row_list_has_somewhere_to_explain_it() -> None:
    source = _MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    missing = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and "yield SettingsList(" in (ast.get_source_segment(source, node) or "")
        and "yield DetailStrip()" not in (ast.get_source_segment(source, node) or "")
    ]
    assert not missing, f"row lists with no DetailStrip to describe the focused row: {missing}"


@pytest.mark.asyncio
async def test_the_source_form_paints_the_focused_row_description(tmp_path: Path) -> None:
    corpus = tmp_path / "notes"
    corpus.mkdir()
    (corpus / "a.md").write_text("# a\n\nbody\n", encoding="utf-8")
    config = Config(collections={"notes": CollectionConfig(sources=[SourceConfig(path=corpus)])})
    app = FNDApp(index_dir=tmp_path / "idx", config=config)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        for _ in range(25):
            await pilot.pause()
        # Land on the Index filters row, which carries a description.
        await pilot.press("down", "down")
        for _ in range(6):
            await pilot.pause()
        painted = re.sub(
            r"\s+",
            " ",
            " ".join(
                "".join(seg.text for seg in strip)
                for strip in app.screen._compositor.render_strips()
            ),
        )

    assert "Ignore files, skipped tags" in painted, painted[-400:]


def _descriptions(app: Any) -> list[str]:
    from fnd.tui.settings_screen import SettingsList

    return [it.description for it in app.screen.query_one(SettingsList)._items if it.description]


@pytest.mark.asyncio
async def test_the_rows_had_the_text_all_along(tmp_path: Path) -> None:
    """The control: this is a placement fix, not new prose. If the rows ever
    stop carrying descriptions, the strip is empty and this says so."""
    corpus = tmp_path / "notes"
    corpus.mkdir()
    config = Config(collections={"notes": CollectionConfig(sources=[SourceConfig(path=corpus)])})
    app = FNDApp(index_dir=tmp_path / "idx", config=config)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        for _ in range(25):
            await pilot.pause()
        described = _descriptions(app)

    assert len(described) >= 4, described
