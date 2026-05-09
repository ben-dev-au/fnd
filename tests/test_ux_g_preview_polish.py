"""UX-G: heading-trim + match minimap + per-line markdown highlight."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static, Tree

from acorn.config import Config, load
from acorn.index import build_index
from acorn.query import Hit
from acorn.tui import AcornApp
from acorn.tui.app import _format_hit_label, _trim_redundant_heading


def test_trim_redundant_heading_strips_filename_prefix() -> None:
    """``Templates > Iterators`` on file ``Templates - Notes.md`` should
    drop the leading ``Templates`` segment — it just repeats the filename
    above. The deeper segment is what locates the match."""
    out = _trim_redundant_heading(
        "Templates > Iterators", title="Templates", path="/x/Templates - Notes.md"
    )
    assert out == "Iterators"


def test_trim_redundant_heading_drops_when_only_filename() -> None:
    """A single-segment heading equal to the filename leaves nothing
    useful — caller falls back to a chunk locator."""
    out = _trim_redundant_heading("Templates", title="Templates", path="/x/Templates.md")
    assert out == ""


def test_trim_redundant_heading_keeps_distinct_segments() -> None:
    """Headings that don't repeat the filename pass through verbatim."""
    out = _trim_redundant_heading(
        "Methods > Soft breaking", title="MSSM review", path="/x/mssm-review.pdf"
    )
    assert out == "Methods > Soft breaking"


def test_format_hit_label_uses_chunk_locator_when_heading_redundant() -> None:
    """Single-heading markdown chunks fall back to ``chunk N`` so the
    section row never just repeats the filename."""
    h = Hit(
        score=1.0,
        parent_id="x",
        path="/x/Templates - Notes.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="Templates",
        title="Templates",
        snippet="strategy pattern",
        chunk_seq=2,
    )
    label = _format_hit_label(h, max_score=1.0)
    plain = label.plain  # rich.Text
    assert "chunk 3" in plain, plain
    assert "§ Templates" not in plain, plain


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def md_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Multi-chunk markdown so the minimap has match + non-match cells."""
    a = tmp_path / "notes"
    body = "\n".join(
        [
            "# Notes",
            "",
            "## With match",
            "the magic anchor word: glimmer is here.",
            "",
            "## Without match",
            "this section says nothing relevant.",
            "",
            "## Also matches",
            "more content with glimmer in it.",
        ]
    )
    _write_md(a / "Notes.md", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_minimap_widget_mounts(cfg_one: Config, md_index: Path) -> None:
    """The minimap is part of the layout regardless of query state."""
    app = AcornApp(index_dir=md_index, config=cfg_one)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Should not raise.
        app.query_one("#match_minimap", Static)


@pytest.mark.asyncio
async def test_minimap_paints_when_chunks_have_matches(cfg_one: Config, md_index: Path) -> None:
    """After a match-bearing query renders, the minimap content is non-empty."""
    app = AcornApp(
        index_dir=md_index,
        config=cfg_one,
        collection="notes",
        initial_query="glimmer",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        # Drive a real cursor move so NodeHighlighted fires and the
        # preview chunks actually mount.
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        # Force the minimap refresh — `call_after_refresh` already
        # scheduled it, but pytest's pilot doesn't always flush that
        # callback in time, so we invoke directly to keep the test
        # deterministic.
        app._refresh_minimap()
        await pilot.pause()
        minimap = app.query_one("#match_minimap", Static)
        # ``Static.render()`` returns the current renderable (a Rich
        # Text in our case); ``.plain`` is its un-styled string form.
        rendered = minimap.render()
        text = getattr(rendered, "plain", str(rendered))
        assert "█" in text, text


@pytest.mark.asyncio
async def test_md_match_chunk_uses_per_line_layout(cfg_one: Config, md_index: Path) -> None:
    """Markdown chunks with matches mount per-line so word highlights work."""
    app = AcornApp(
        index_dir=md_index,
        config=cfg_one,
        collection="notes",
        initial_query="glimmer",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        pane = app.query_one("#preview_pane", VerticalScroll)
        # Per-line layout: at least one Static carries the
        # ``chunk-line-match`` class for a match-bearing line. The old
        # markdown-only path mounted a single Static per chunk, so this
        # would never appear there.
        match_lines = [w for w in pane.query(Static) if "chunk-line-match" in w.classes]
        assert match_lines, "expected per-line match highlight on md chunks"
