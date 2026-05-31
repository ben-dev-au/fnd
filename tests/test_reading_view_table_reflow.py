"""Bug B: a wide table in the structural preview must widen to use the extra
space when Reading View hides the sidebar.

The live preview renders markdown tables via ``FNDMarkdownTableDT`` → a
``DataTable`` whose column widths are computed once at compose from the
(narrow) pane width. Toggling Reading View widens the pane; ``on_resize``
now recomputes the column widths so the table reflows wider instead of
staying compressed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable

from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


def _table_doc(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir()
    rows = [
        "| Pattern | Intent description column | Consequences description column | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for i in range(12):
        rows.append(
            f"| Strategy{i} | Encapsulates interchangeable algorithms behind one "
            f"interface | Lets the algorithm vary independently from clients that "
            f"use it | quartzfin-anchor row {i} |"
        )
    (notes / "doc.md").write_text(
        "# Patterns table\n\nIntro.\n\n" + "\n".join(rows) + "\n", encoding="utf-8"
    )
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _col_total(app: FNDApp) -> int | None:
    tables = list(app.query(DataTable))
    if not tables:
        return None
    cols = list(tables[0].columns.values())
    if not cols or any(c.width is None for c in cols):
        return None
    return sum(c.width for c in cols)


@pytest.mark.skip(
    reason="Under app.run_test the markdown table preview renders via the flat "
    "LineBufferPreview path, not the W3 DataTable this fix targets — so the "
    "DataTable never mounts headlessly. Needs live (tmux/real app) verification. "
    "The on_resize column-width recompute is the fix under test."
)
@pytest.mark.asyncio
async def test_reading_view_widens_table(tmp_path: Path, tmp_index_dir: Path) -> None:
    index = _table_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin-anchor")
    async with app.run_test(size=(120, 40)) as pilot:
        from textual.widgets import Tree

        await wait_until(pilot, lambda: bool(app._groups), timeout=15.0, message="no results")
        app.query_one("#results_pane", Tree).focus()
        await wait_until(
            pilot,
            lambda: app._active_preview is not None and _col_total(app) is not None,
            timeout=20.0,
            message="DataTable never rendered in the structural preview",
        )
        before = _col_total(app)
        assert before is not None

        app.action_toggle_reading_mode()
        pane = app.query_one("#preview_pane")
        await wait_until(
            pilot,
            lambda: pane.size.width > 100,
            timeout=15.0,
            message="preview pane never widened after Reading View toggle",
        )
        await wait_until(
            pilot,
            lambda: (_col_total(app) or 0) > before,
            timeout=15.0,
            message=f"table columns never widened (stuck at {before})",
        )
        assert (_col_total(app) or 0) > before
