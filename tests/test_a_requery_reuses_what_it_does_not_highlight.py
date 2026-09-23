"""A new query reuses the captures of chunks neither query highlights.

Re-querying one file rebuilt every chunk of it, though most chunks paint the
same under any query that matches nothing in them. Reuse must never serve a
chunk the new query DOES highlight: that would show it without its match.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.strip import Strip

from fnd.index import build_index
from fnd.matching import MatchSpec
from fnd.tui import FNDApp
from fnd.tui.preview.frozen import FrozenChunk, FrozenChunkView, stands_in_for, style_key
from fnd.tui.preview.frozen_store import ChunkCaptureStore
from fnd.tui.widgets.markdown import FNDMarkdown
from tests._pilot_wait import safe_press, wait_until

KEY = style_key("tokyo-night", render_mermaid=True)


def _plain(source: str, plains: tuple[str, ...], *, seq: int = 1) -> FrozenChunk:
    return FrozenChunk(
        chunk_seq=seq,
        width=80,
        strips=[Strip.blank(80)],
        source=source,
        block_plains=plains,
        painted_match=False,
        style_key=KEY,
    )


def test_a_capture_stands_in_only_where_nothing_would_be_highlighted() -> None:
    capture = _plain("Some prose about zebras.", ("Some prose about zebras.",))
    source = capture.source
    assert source is not None

    assert stands_in_for(capture, source, MatchSpec.from_query("giraffe"), KEY)
    assert stands_in_for(capture, source, MatchSpec(), KEY)
    assert not stands_in_for(capture, source, MatchSpec.from_query("zebra"), KEY)
    assert not stands_in_for(capture, source + " changed", MatchSpec.from_query("giraffe"), KEY)
    assert not stands_in_for(
        capture, source, MatchSpec.from_query("giraffe"), style_key("other", render_mermaid=True)
    )


def test_a_capture_that_highlighted_something_never_stands_in() -> None:
    capture = _plain("Some prose.", ("Some prose.",))
    capture.painted_match = True
    assert not stands_in_for(capture, "Some prose.", MatchSpec.from_query("giraffe"), KEY)


def test_text_the_render_hides_still_counts_as_a_match() -> None:
    """An Obsidian comment is hidden unless it holds a match, so its words are
    absent from the rendered text the capture recorded."""
    source = "Visible prose. %%secret okapi%%"
    capture = _plain(source, ("Visible prose.",))
    assert not stands_in_for(capture, source, MatchSpec.from_query("okapi"), KEY)


def test_the_plain_captures_outlive_a_new_query_and_the_rest_do_not() -> None:
    store = ChunkCaptureStore()
    plain = _plain("a", ("a",), seq=1)
    painted = _plain("b", ("b",), seq=2)
    painted.painted_match = True
    store.put("p", "q1|hl=1", 80, plain)
    store.put("p", "q1|hl=1", 80, painted)

    store.clear_query_captures()

    assert store.get("p", "q1|hl=1", 80, 1) is None
    assert store.get_plain("p", 80, 1) is plain
    assert store.get_plain("p", 80, 2) is None


def _sections(tmp_path: Path, index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir()
    lines = ["# Doc", ""]
    for i in range(60):
        lines.append(f"## Section {i}")
        word = {5: "alphaword", 40: "betaword"}.get(i, "filler")
        lines.append(f"Body of section {i} with {word} in it.")
        lines.extend(f"More prose line {n} for section {i}." for n in range(3))
        lines.append("")
    (notes / "doc.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=index_dir, collection="notes")
    return index_dir


@pytest.mark.asyncio
async def test_a_requery_serves_the_unmatched_chunks_and_builds_the_matched_one(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    index = _sections(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="alphaword")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="the first query never showed a preview",
        )
        parent_id = app._search.groups[0].parent_id
        store = app._preview.capture_store
        width = app._preview.capture_width(app.query_one("#preview_pane", VerticalScroll))
        searcher = app._search.searcher
        assert searcher is not None
        beta_seq = next(
            c.chunk_seq for c in searcher.get_file_chunks(parent_id) if "betaword" in c.body_md
        )
        # Precondition: the first query captured the chunk the second will match,
        # unhighlighted, so reuse is on offer and must be refused.
        await wait_until(
            pilot,
            lambda: store.get_plain(parent_id, width, beta_seq) is not None,
            timeout=30.0,
            message="the first query's sweep never captured the far chunk",
        )

        app.query_one("#query_bar").focus()
        app.query_one("#query_bar").value = "betaword"  # type: ignore[attr-defined]
        await safe_press(pilot, "enter")
        await wait_until(
            pilot,
            lambda: (
                app._search.current_query == "betaword"
                and app._preview.active is not None
                and app._preview.is_painted()
                and beta_seq in app._preview.active.chunk_widgets
            ),
            timeout=20.0,
            message="the second query never showed its match",
        )

        container = app._preview.active
        assert container is not None
        matched = container.chunk_widgets[beta_seq]
        assert isinstance(matched, FNDMarkdown), (
            f"the chunk the new query matches came back as {type(matched).__name__}: an "
            "earlier query's unhighlighted capture was served in its place"
        )
        assert matched.first_match_block is not None, "the matched chunk shows no match"
        assert container.served_chunks > 0, "nothing was reused; every chunk was rebuilt"
        stale = [
            seq
            for seq, view in container.chunk_widgets.items()
            if isinstance(view, FrozenChunkView)
            and not view.frozen.painted_match
            and any("betaword" in p for p in view.frozen.block_plains)
        ]
        assert not stale, f"chunks {stale} show the current match without its highlight"
