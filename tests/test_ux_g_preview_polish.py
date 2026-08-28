"""UX-G: heading-trim + match minimap + per-line markdown highlight."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.query import Hit
from fnd.tui import FNDApp
from fnd.tui.results_labels import _format_hit_label, _trim_redundant_heading
from tests._pilot_wait import wait_until


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


def test_hit_label_replaces_tab_so_pane_border_cannot_break() -> None:
    """A snippet carrying a raw TAB (common in extracted PDF body text, e.g.
    ``5.\\tExplain ...``) must not reach the results-row label verbatim: a
    terminal expands ``\\t`` to the next tab stop while Rich measures it as
    zero cells, so the row over-runs its content region and corrupts the pane's
    right border. Every whitespace char in a label must be a plain space."""
    h = Hit(
        score=1.0,
        parent_id="x",
        path="/x/Koffman.pdf",
        kind="pdf",
        page=344,
        slide=0,
        heading_path="",
        title="Koffman",
        snippet="of null. 5.\tExplain what is wrong",
        page_label="344",
        chunk_seq=0,
    )
    plain = _format_hit_label(h, max_score=1.0).plain
    assert "\t" not in plain, plain
    # The tab collapses to a single space, so the text reads cleanly and the
    # rendered width matches the measured width (no zero-width surprise).
    assert "5. Explain" in plain, plain


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
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
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
    (FNDMarkdown), with the matched word carrying a search-highlight
    span on its block's Content. Replaces the legacy per-line layout
    assertion — the structural renderer keeps tables / fenced code /
    lists rendering correctly even when a chunk contains a match.
    """
    from fnd.tui.widgets.markdown import FNDMarkdown

    app = FNDApp(
        index_dir=md_index,
        config=cfg_one,
        collection="notes",
        initial_query="glimmer",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        pane = app.query_one("#preview_pane", VerticalScroll)

        def any_highlight() -> bool:
            """A block under some FNDMarkdown carries a search-highlight span —
            the visible match indicator. The widget mounting is not the span
            landing: Windows rendered one a frame before the other."""
            for md in pane.query(FNDMarkdown):
                for block in md.query("MarkdownBlock"):
                    content = getattr(block, "_content", None)
                    if content is not None and any("bold" in str(sp.style) for sp in content.spans):
                        return True
            return False

        await wait_until(
            pilot,
            any_highlight,
            timeout=30.0,
            message="no block ever carried a search-highlight span",
        )
        assert list(pane.query(FNDMarkdown)), "expected matched md chunk to mount FNDMarkdown"
        assert any_highlight(), "expected a highlight span on at least one block"
