"""Three claims on the Indexing screens that their own numbers contradict.

`Files in index` counted 16 files including `data.csv`, `script.py` and
`page.html` while naming "(md, pptx, docx, txt, PDFs)". `PDFs textured`
explained a `Y - X` formula using letters the row never prints, and named an
index field at the user. The engine row said every PDF stays flat without it,
on a screen simultaneously reporting 2 of 2 textured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.kinds import KIND_BY_ID
from fnd.tui import FNDApp
from fnd.tui.menu import SECTION_INDEXING, SECTION_PDF_TEXTURE
from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings_section
from tests._pilot_wait import settings_ready

#: Names of index fields. A settings row explains what the user sees, not the
#: schema underneath it.
_SCHEMA_LEAKS = ("body_md", "body_struct", "parent_id", "chunk_seq")


async def _descriptions(app: FNDApp, pilot: Any, section: str) -> list[tuple[str, str]]:
    open_settings_section(app, section)
    await settings_ready(pilot, app)
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    return [(it.id, it.description or "") for it in screen.query_one(SettingsList)._items]


@pytest.mark.asyncio
@pytest.mark.parametrize("section", [SECTION_INDEXING, SECTION_PDF_TEXTURE])
async def test_no_row_names_an_index_field(section: str, tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        rows = await _descriptions(app, pilot, section)

    leaks = [(rid, w) for rid, text in rows for w in _SCHEMA_LEAKS if w in text]
    assert not leaks, f"schema names shown to the user: {leaks}"


@pytest.mark.asyncio
async def test_no_row_enumerates_a_subset_of_what_it_counts(tmp_index_dir: Path) -> None:
    """Naming five types over a number that counts forty is the defect; naming
    none is fine, and so would naming all of them be."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        rows = await _descriptions(app, pilot, SECTION_INDEXING)

    wrong = []
    for rid, text in rows:
        if rid not in ("indexing.files_in_index", "indexing.update_all_index_only"):
            continue
        named = {k for k in KIND_BY_ID if f"{k}," in text or f"{k})" in text}
        if named and named != set(KIND_BY_ID):
            wrong.append((rid, sorted(named)))
    assert not wrong, f"rows naming a subset of the kinds they count: {wrong}"


@pytest.mark.asyncio
async def test_the_engine_row_does_not_claim_every_pdf_stays_flat(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        rows = dict(await _descriptions(app, pilot, SECTION_PDF_TEXTURE))

    engine = rows["pdf_texture.engine_status"]
    assert "every PDF stays flat" not in engine, engine
    assert "texturis" in engine.lower(), "the control: it still says what the engine is for"
