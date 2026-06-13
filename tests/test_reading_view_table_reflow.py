"""Bug B: a wide table in the structural preview widens to use the extra
space when Reading View hides the sidebar.

The live preview renders markdown tables via ``FNDMarkdownTableDT`` → a
``DataTable`` whose column widths are computed from the pane width and
recomputed by ``on_resize`` when the pane widens. The reflow's core is the
pure ``_compute_table_col_widths`` function — unit-tested here headlessly.
The full-app integration runs under ``app.run_test``: the DataTable preview
path is the live default (``FNDMarkdown.BLOCKS["table_open"]``) and DOES
mount headless, verified 2026-06-13. (It was previously gated behind
``FND_LIVE_PREVIEW_TESTS`` on the stale assumption that it didn't.)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import DataTable

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.widgets.markdown import _compute_table_col_widths
from tests._pilot_wait import wait_until


def _wide_table() -> tuple[list[Text], list[list[Text]]]:
    headers = [
        Text("Pattern"),
        Text("Intent description column"),
        Text("Consequences description column"),
        Text("Notes"),
    ]
    rows = [
        [
            Text(f"Strategy{i}"),
            Text("Encapsulates interchangeable algorithms behind one interface"),
            Text("Lets the algorithm vary independently from clients that use it"),
            Text(f"anchor row {i}"),
        ]
        for i in range(12)
    ]
    return headers, rows


def test_wider_available_width_widens_columns() -> None:
    """The reflow's core: the same table given more width returns wider
    columns (cells that wrapped in the narrow pane no longer need to)."""
    headers, rows = _wide_table()
    narrow = _compute_table_col_widths(headers, rows, available_width=48)
    wide = _compute_table_col_widths(headers, rows, available_width=120)
    assert narrow
    assert wide
    assert len(narrow) == len(wide) == len(headers)
    assert sum(wide) > sum(narrow)
    # widening never shrinks a column.
    assert all(w >= n for w, n in zip(wide, narrow, strict=True))


def test_widths_fit_within_available() -> None:
    """When content overflows, columns (+ per-cell padding) stay within the
    available width so the table never paints past the pane."""
    headers, rows = _wide_table()
    avail, pad = 60, 1
    widths = _compute_table_col_widths(headers, rows, available_width=avail, cell_padding=pad)
    assert sum(widths) + 2 * pad * len(widths) <= avail


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
    if not cols:
        return None
    return sum(c.width for c in cols)


@pytest.mark.asyncio
async def test_reading_view_widens_table(tmp_path: Path, tmp_index_dir: Path) -> None:
    index = _table_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin-anchor")
    async with app.run_test(size=(120, 40)) as pilot:
        from textual.widgets import Tree

        await wait_until(
            pilot, lambda: bool(app._search.groups), timeout=15.0, message="no results"
        )
        app.query_one("#results_pane", Tree).focus()
        await wait_until(
            pilot,
            lambda: app._preview.active is not None and _col_total(app) is not None,
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
