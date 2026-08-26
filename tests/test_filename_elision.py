"""Filenames middle-elide so the extension stays visible in the preview
title and result rows, instead of the terminal clipping it off the right."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Tree

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.results_labels import _elide_middle_keep_suffix
from tests._pilot_wait import preview_landed

# ── Pure helper ──────────────────────────────────────────────────────


def test_short_name_unchanged() -> None:
    assert _elide_middle_keep_suffix("notes.md", 40) == "notes.md"


def test_elides_middle_and_keeps_extension() -> None:
    out = _elide_middle_keep_suffix("really_long_report_final_v3.pdf", 18)
    assert out.endswith(".pdf"), out
    assert "…" in out, out
    assert len(out) == 18, out
    assert out.startswith("really"), out  # leading context preserved


def test_never_exceeds_budget() -> None:
    name = "a_very_long_filename_that_keeps_going_and_going.markdown"
    for w in range(2, len(name) + 2):
        out = _elide_middle_keep_suffix(name, w)
        assert len(out) <= w, (w, out)


def test_keeps_extension_at_realistic_narrow_budget() -> None:
    out = _elide_middle_keep_suffix("Quarterly Report Final Draft v3.docx", 16)
    assert out.endswith(".docx"), out
    assert "…" in out, out


def test_no_extension_still_middle_elides() -> None:
    out = _elide_middle_keep_suffix("MAKEFILE_WITHOUT_ANY_SUFFIX", 11)
    assert "…" in out, out
    assert len(out) <= 11, out


def test_extension_longer_than_budget_degrades_gracefully() -> None:
    # Suffix can't fit alongside an ellipsis — must not crash or over-run.
    out = _elide_middle_keep_suffix("x.markdown", 4)
    assert len(out) <= 4, out


def test_ellipsis_plus_suffix_fits_at_exact_boundary() -> None:
    # max_width == len(suffix) + 1: exactly room for "…" + the full
    # extension and no stem — must show "…<ext>", not drop the extension.
    assert _elide_middle_keep_suffix("longfile.txt", 5) == "….txt"
    assert _elide_middle_keep_suffix("report.docx", 6) == "….docx"


# ── Integration: real app, narrow terminal ───────────────────────────

_LONG_STEM = "this_is_a_really_long_document_name_for_testing_extension_elision"


@pytest.fixture
def long_name_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / f"{_LONG_STEM}.md").write_text(
        "# Heading\n\nThe magic search token is zorptastic in this document.\n",
        encoding="utf-8",
    )
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_preview_title_keeps_extension_when_narrow(long_name_index: Path) -> None:
    app = FNDApp(index_dir=long_name_index, initial_query="zorptastic")
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await preview_landed(pilot, app)

        preview = app.query_one("#preview_pane", VerticalScroll)
        title = str(preview.border_title or "")
        assert ".md" in title, title  # extension survived
        assert "…" in title, title  # proof it was middle-elided
        # The painted title must fit the edge so the terminal doesn't
        # re-truncate it (which is what eats the extension today).
        assert len(title) <= preview.region.width - 6, (len(title), preview.region.width)


@pytest.mark.asyncio
async def test_file_row_keeps_extension_when_narrow(long_name_index: Path) -> None:
    app = FNDApp(index_dir=long_name_index, initial_query="zorptastic")
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        file_node = next(iter(tree.root.children))
        label = str(file_node.label)
        assert ".md" in label, label
        assert "…" in label, label


@pytest.mark.asyncio
async def test_wide_terminal_shows_full_name(long_name_index: Path) -> None:
    app = FNDApp(index_dir=long_name_index, initial_query="zorptastic")
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await preview_landed(pilot, app)
        preview = app.query_one("#preview_pane", VerticalScroll)
        title = str(preview.border_title or "")
        assert _LONG_STEM in title, title  # full stem fits, no elision
        assert "…" not in title, title
