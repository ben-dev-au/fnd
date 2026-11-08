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


def test_a_longer_match_over_a_window_edge_paints_whole() -> None:
    """One window ends inside "testing"; the next window's whole word still paints."""
    fv = build_file_view(
        [(0, "aa testi", [(3, 7, "bold")]), (1, "testing b", [(0, 7, "bold")])], repeats=[5]
    )
    assert [line.plain for line in fv.lines] == ["aa testing b"]
    painted = sorted({i for sp in fv.lines[0].spans for i in range(sp.start, sp.end)})
    assert painted == list(range(3, 10))


def test_a_chunk_lands_on_the_match_painted_full_in_its_window() -> None:
    """Window 1's head pair is full only in window 0, and that is what it paints there."""
    dim = next(iter(DIM_STYLES))
    first, second = "aaa alpha\nbeta", "beta zzz\nqqq alpha beta"
    fv = build_file_view(
        [
            (0, first, [(4, 9, "bold"), (10, 14, "bold")]),
            (1, second, [(0, 4, dim), (13, 18, "bold"), (19, 23, "bold")]),
        ],
        repeats=[4],
    )
    assert [line.plain for line in fv.lines] == ["aaa alpha", "beta zzz", "qqq alpha beta"]
    assert fv.first_hit_line_in_chunk[1] == 1


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
        first, second = sorted(row.children, key=lambda n: n.data["hit"].chunk_seq)
        tree.move_cursor(first)

        def shows(marker: str) -> bool:
            anchor = app._preview_scroll.anchor
            return (
                anchor is not None
                and anchor.parent_id == parent
                and app._preview.is_painted()
                and not app._preview.pipeline_busy()
                and not app._preview_scroll.is_settling
                and any(marker in r for r in _preview_rows(app))
            )

        await wait_until(pilot, lambda: shows("row 10 zebre"), timeout=20.0, message="no landing")
        assert all(
            h.pass_index == FUZZY_PASS_INDEX
            for g in app._search.groups
            for h in g.hits
            if "p.py" in g.path
        )
        for _ in range(4):
            if tree.cursor_node is second:
                break
            app.action_nav_next_match()
            await settle(pilot)
        assert tree.cursor_node is second
        await wait_until(pilot, lambda: shows("row 200 zebre"), timeout=20.0, message="no row 200")


@pytest.mark.asyncio
async def test_a_phrase_across_a_fence_cut_stops_in_both_fences(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 147 ends one fence and row 148 opens the next: both mounted fences paint
    their half, and each has a stop row live and frozen."""
    monkeypatch.setenv("_FND_NO_FREEZE", "1")
    from fnd.tui.preview.frozen import freeze
    from fnd.tui.preview.match_row import chunk_stop_rows
    from fnd.tui.widgets.markdown import FNDMarkdown

    rows = [f"x_{i} = {i}" for i in range(400)]
    rows[147] += "  # alpha"
    rows[148] = "beta = 148"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "prog.py").write_text("\n".join(rows), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    app = FNDApp(index_dir=tmp_index_dir, initial_query='"alpha beta"')
    async with app.run_test(size=(120, 30)) as pilot:

        def fences() -> list[FNDMarkdown]:
            active = app._preview.active
            if active is None or app._preview.pipeline_busy():
                return []
            found = [active.chunk_widgets.get(seq) for seq in (0, 1)]
            return [w for w in found if isinstance(w, FNDMarkdown) and w.build_done.is_set()]

        await wait_until(pilot, lambda: len(fences()) == 2, timeout=20.0, message="no fences")
        await settle(pilot)
        painted = []
        for md in fences():
            spans = [sp for b in md.query("*") for sp in getattr(b, "_fnd_match_spans", None) or []]
            plain = md.block_plains[0]
            painted.append({plain[sp.start : sp.end].strip() for sp in spans})
            assert chunk_stop_rows(md, md.match_spec)[0]
            frozen = freeze(md, 0)
            assert frozen is not None
            assert frozen.stop_rows
        assert "alpha" in painted[0]
        assert "beta" in painted[1]
