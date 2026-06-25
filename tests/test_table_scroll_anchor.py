"""A match deep in a table must scroll to the matched ROW, not the table top.

The W3 table renders every row as one full-height ``DataTable`` (no per-cell
widgets, no internal scroll), so the matched cell is not its own scrollable
widget. ``StructuralScrollStrategy._anchor_region`` resolves the cell's screen
region as the scroll anchor — and returns ``None`` (a retry signal) while the
table's rows are still mounting, so the controller retries instead of
committing a scroll to the table top. That cold-mount race is what repeatedly
stranded deep-table matches at the top; these lock the contract that prevents
it returning the table's own (top) region on a not-ready cell.
"""

from __future__ import annotations

from typing import Any

from textual.geometry import Offset, Region
from textual.widgets import DataTable

from fnd.tui.preview_scroll import StructuralScrollStrategy


def _strategy() -> StructuralScrollStrategy:
    # _anchor_region / _match_table_for read nothing off the host.
    return StructuralScrollStrategy(host=object())  # type: ignore[arg-type]


class _FakeTable:
    """Duck-typed stand-in exposing just what ``_anchor_region`` reads."""

    def __init__(self, cell: Region | Exception, *, region: Region) -> None:
        self._fnd_match_coord = (5, 1)
        self._cell = cell
        self.region = region
        self.scroll_offset = Offset(0, 0)

    def _get_cell_region(self, _coord: Any) -> Region:
        if isinstance(self._cell, Exception):
            raise self._cell
        return self._cell


class _PlainWidget:
    region = Region(0, 200, 80, 3)


def test_non_table_anchor_is_the_targets_own_region() -> None:
    w = _PlainWidget()
    assert _strategy()._anchor_region(w, None) is w.region  # type: ignore[arg-type]


def test_resolved_anchor_is_the_matched_cell_not_the_table_top() -> None:
    # Table sits at screen y=100; the matched cell is 300 rows into its content.
    table = _FakeTable(cell=Region(0, 300, 20, 1), region=Region(0, 100, 80, 500))
    anchor = _strategy()._anchor_region(table, table)  # type: ignore[arg-type]
    assert anchor is not None
    # cell.translate(table.region.offset) → 300 + 100 = 400: the matched row,
    # well below the table top (y=100), not the top.
    assert anchor.y == 400
    assert anchor.y != table.region.y


def test_anchor_is_none_when_cell_region_not_ready_raises() -> None:
    # Rows not mounted yet → _get_cell_region raises. Must signal retry (None),
    # NOT fall back to the table's own top region.
    table = _FakeTable(cell=RuntimeError("rows not mounted"), region=Region(0, 100, 80, 500))
    assert _strategy()._anchor_region(table, table) is None  # type: ignore[arg-type]


def test_anchor_is_none_when_cell_region_zero_height() -> None:
    # A clamped/zero-height cell region means the table isn't sized yet.
    table = _FakeTable(cell=Region(0, 0, 20, 0), region=Region(0, 100, 80, 500))
    assert _strategy()._anchor_region(table, table) is None  # type: ignore[arg-type]


def test_match_table_for_requires_a_match_coordinate() -> None:
    s = _strategy()
    dt: DataTable[Any] = DataTable()
    # A table with no recorded match coordinate is treated as a plain widget.
    assert s._match_table_for(dt) is None
    dt._fnd_match_coord = (3, 0)  # type: ignore[attr-defined]
    assert s._match_table_for(dt) is dt


def test_match_table_for_ignores_non_tables() -> None:
    assert _strategy()._match_table_for(_PlainWidget()) is None  # type: ignore[arg-type]
