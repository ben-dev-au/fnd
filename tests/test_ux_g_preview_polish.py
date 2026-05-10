"""UX-G: heading-trim + match minimap + per-line markdown highlight."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Tree

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


def test_trim_redundant_heading_strips_word_in_long_filename() -> None:
    """The filename can carry many words; if the leading heading segment
    matches one of them it should still be stripped."""
    out = _trim_redundant_heading(
        "Templates > Strategy",
        title="DPC Wk8 Notes - Templates, Strategy & C++ Streams",
        path="/x/DPC Wk8 Notes - Templates, Strategy & C++ Streams.md",
    )
    assert out == "Strategy"


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
    # Single-segment heading equal to the filename collapses to a
    # ``§N`` chunk locator (no leading ``§ Templates`` since that just
    # repeats the file row above).
    assert "§3" in plain, plain
    assert "Templates" not in plain.split("strategy")[0], plain


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
async def test_md_match_chunk_renders_via_markdown_widget_with_highlight(
    cfg_one: Config, md_index: Path
) -> None:
    """Matched markdown chunks render via Textual's Markdown widget tree
    (AcornMarkdown), with the matched word carrying a search-highlight
    span on its block's Content. Replaces the legacy per-line layout
    assertion — the structural renderer keeps tables / fenced code /
    lists rendering correctly even when a chunk contains a match.
    """
    from acorn.tui.app import AcornMarkdown

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
        await pilot.pause(0.3)
        pane = app.query_one("#preview_pane", VerticalScroll)
        md_widgets = list(pane.query(AcornMarkdown))
        assert md_widgets, "expected matched md chunk to mount AcornMarkdown"
        # Some block under at least one AcornMarkdown carries a
        # search-highlight span — that's the visible match indicator.
        any_highlight = False
        for md in md_widgets:
            for block in md.query("MarkdownBlock"):
                spans = getattr(block, "_content", None)
                if spans is None:
                    continue
                if any("bold" in str(s.style) for s in spans.spans):
                    any_highlight = True
                    break
            if any_highlight:
                break
        assert any_highlight, "expected a highlight span on at least one block"
