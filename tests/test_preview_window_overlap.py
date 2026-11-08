"""The preview shows an extractor's window overlap once, and every section still
lands on its match."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.text import Text
from textual.widgets import Tree

from fnd.index import build_index
from fnd.render import DIM_STYLES
from fnd.tui import FNDApp
from fnd.tui.line_buffer import LineBufferPreview, build_file_view
from fnd.tui.match_evidence import FUZZY_PASS_INDEX
from tests._pilot_wait import settle, wait_until


def _styled(line: Text) -> list[tuple[int, int]]:
    return [(s.start, s.end) for s in line.spans]


def test_a_repeat_shows_once_and_its_line_runs_on() -> None:
    """Two windows sharing a stretch read as the source, with no gap row."""
    src = "alpha beta\ngamma delta\nepsilon zeta"
    first, second = src[:16], src[12:]
    fv = build_file_view([(0, first, []), (1, second, [])], repeats=[4])
    assert [line.plain for line in fv.lines] == src.split("\n")
    assert fv.chunk_to_range == {0: (0, 2), 1: (1, 3)}
    assert fv.line_to_chunk == [0, 0, 1]


def test_a_word_cut_by_the_boundary_paints_whole() -> None:
    """The earlier window holds the whole word, so its span paints across the cut."""
    src = "one two zebra three"
    first, second = src[:14], src[9:]
    fv = build_file_view([(0, first, [(8, 13, "bold")]), (1, second, [])], repeats=[5])
    assert [line.plain for line in fv.lines] == [src]
    assert _styled(fv.lines[0]) == [(8, 13)]
    assert fv.first_hit_line_in_chunk == {0: 0}


def test_a_match_in_the_repeat_paints_once_and_lands_both_chunks() -> None:
    """Both windows see the match; it paints once and each chunk lands on it."""
    first, second = "aaa\nbbb\nzebra", "zebra\nccc"
    fv = build_file_view(
        [(0, first, [(8, 13, "bold")]), (1, second, [(0, 5, "bold")])], repeats=[5]
    )
    assert [line.plain for line in fv.lines] == ["aaa", "bbb", "zebra", "ccc"]
    assert _styled(fv.lines[2]) == [(0, 5)]
    assert fv.first_hit_line_in_chunk == {0: 2, 1: 2}
    assert fv.match_lines == {2}


@pytest.mark.parametrize("dim_side", [0, 1])
def test_a_shared_match_paints_full_when_either_window_qualifies(dim_side: int) -> None:
    """A proximity term dimmed in one window paints full where the other has its pair."""
    dim = next(iter(DIM_STYLES))
    first, second = "aaa alpha\nbeta", "beta zzz"
    styles = ["bold", "bold"]
    styles[dim_side] = dim
    fv = build_file_view(
        [(0, first, [(4, 9, "bold"), (10, 14, styles[0])]), (1, second, [(0, 4, styles[1])])],
        repeats=[4],
    )
    assert [line.plain for line in fv.lines] == ["aaa alpha", "beta zzz"]
    assert [(sp.start, sp.end, str(sp.style)) for sp in fv.lines[1].spans] == [(0, 4, "bold")]


@pytest.fixture
def txt_index(tmp_path: Path, tmp_index_dir: Path) -> tuple[Path, list[str]]:
    docs = tmp_path / "docs"
    docs.mkdir()
    lines = [f"line {i} filler words about nothing much" for i in range(400)]
    lines[200] = "line 200 zebra marker"
    (docs / "notes.txt").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir, lines


@pytest.mark.asyncio
async def test_a_txt_preview_shows_the_file_once(txt_index: tuple[Path, list[str]]) -> None:
    """The flat buffer holds the file's lines exactly and lands on the match."""
    index_dir, lines = txt_index
    app = FNDApp(index_dir=index_dir, initial_query="zebra")
    async with app.run_test(size=(120, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: app._flat.active_buffer is not None and not app._preview_scroll.is_settling,
            timeout=20.0,
            message="the flat preview never landed",
        )
        buf = app._flat.active_buffer
        assert isinstance(buf, LineBufferPreview)
        fv = buf._fv
        assert fv is not None
        assert [line.plain for line in fv.lines] == lines
        assert set(fv.first_hit_line_in_chunk.values()) == {200}


@pytest.fixture
def code_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    rows = [f"x_{i} = {i}  # row {i}" + (" zebra" if i == 150 else "") for i in range(400)]
    (docs / "prog.py").write_text("\n".join(rows), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _preview_rows(app: FNDApp) -> list[str]:
    pane = app.query_one("#preview_pane").region
    strips = app.screen._compositor.render_strips()
    rows = ["".join(s.text for s in strip) for strip in strips[pane.y : pane.bottom]]
    return [row[pane.x : pane.right] for row in rows]


@pytest.mark.asyncio
async def test_each_code_section_lands_on_its_one_shown_match(code_index: Path) -> None:
    """Both windows hold row 150; each section lands with it on screen, shown once."""
    app = FNDApp(index_dir=code_index, initial_query="zebra")
    async with app.run_test(size=(120, 30)) as pilot:
        tree = app.query_one("#results_pane", Tree)
        await wait_until(
            pilot,
            lambda: bool(tree.root.children) and len(tree.root.children[0].children) == 2,
            timeout=20.0,
            message="the overlap match never gave two sections",
        )
        pane = app.query_one("#preview_pane")
        sections = list(tree.root.children[0].children)
        if tree.cursor_node is sections[0]:
            sections.reverse()
        for section in sections:
            await wait_until(
                pilot,
                lambda: app._preview.is_painted() and not app._preview.pipeline_busy(),
                timeout=20.0,
                message="the preview never settled",
            )
            pane.scroll_home(animate=False)
            await wait_until(
                pilot,
                lambda: not any("row 150 zebra" in row for row in _preview_rows(app)),
                message="the preview never left the match",
            )
            tree.move_cursor(section)
            await wait_until(
                pilot,
                lambda: (
                    app._preview.is_painted()
                    and not app._preview.pipeline_busy()
                    and not app._preview_scroll.is_settling
                    and any("row 150 zebra" in row for row in _preview_rows(app))
                ),
                timeout=20.0,
                message="the section never landed on its match",
            )
            rows = _preview_rows(app)
            numbered = [row.split("# row ")[1].split()[0] for row in rows if "# row " in row]
            assert sum("row 150 zebra" in row for row in rows) == 1, rows
            assert len(numbered) == len(set(numbered)), rows


@pytest.mark.asyncio
async def test_b_never_hands_over_to_a_section_whose_match_shows_elsewhere(
    code_index: Path,
) -> None:
    """Section 0's only match shows in chunk 1, so ``b`` from it has nowhere to go."""
    app = FNDApp(index_dir=code_index, initial_query="zebra")
    async with app.run_test(size=(120, 30)) as pilot:

        def landed() -> bool:
            return (
                app._preview.is_painted()
                and not app._preview.pipeline_busy()
                and not app._preview_scroll.is_settling
                and any("row 150 zebra" in row for row in _preview_rows(app))
            )

        await wait_until(pilot, landed, timeout=20.0, message="the match never landed")
        app.action_nav_prev_match()
        assert app._preview.pending_landing_intent is None
        await settle(pilot)
        assert landed()
        assert not app._current_match_unlocatable()


@pytest.mark.asyncio
async def test_a_fuzzy_found_section_stays_a_hand_over_target(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A section the fuzzy pass found is judged by its fuzzy highlight, so n reaches it."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "other.txt").write_text("a zebra here", encoding="utf-8")
    rows = [f"x_{i} = {i}  # row {i}" for i in range(400)]
    rows[10] += " zebre"
    rows[200] += " zebre"
    (docs / "p.py").write_text("\n".join(rows), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    app = FNDApp(index_dir=tmp_index_dir, initial_query="zebra")
    async with app.run_test(size=(120, 30)) as pilot:
        tree = app.query_one("#results_pane", Tree)

        def code_row() -> Any:
            return next(
                (n for n in tree.root.children if "p.py" in str(n.label) and len(n.children) == 2),
                None,
            )

        await wait_until(pilot, lambda: code_row() is not None, timeout=20.0, message="no code row")
        row = code_row()
        parent = row.data["group"].parent_id
        row.expand()
        await settle(pilot)
        tree.move_cursor(row.children[0])

        def loaded() -> bool:
            anchor = app._preview_scroll.anchor
            return (
                anchor is not None
                and anchor.parent_id == parent
                and app._preview.is_painted()
                and not app._preview.pipeline_busy()
                and parent in app._preview.chunk_cache
            )

        await wait_until(pilot, loaded, timeout=20.0, message="the code file never loaded")
        sections = app._match_nav._listed_sections()
        assert len(sections) == 2
        assert all(
            h.pass_index == FUZZY_PASS_INDEX
            for g in app._search.groups
            for h in g.hits
            if "p.py" in g.path
        )
        assert app._match_nav._hop_sections(sections[0]) == sections
