"""Preview scrollbar marker positions are weighted by chunk size.

Regression for markers that were placed by chunk *ordinal* (a bool per
chunk mapped uniformly across the bar), which put a match in a short
chunk following a long one near the top when it actually sat far down —
off by up to half the bar. The fix weights each chunk by its rendered
line count so a match's line fraction tracks its scroll-track fraction.
"""

from __future__ import annotations

from fnd.extract.base import Block
from fnd.matching import MatchSpec
from fnd.query import FileChunk
from fnd.tui.preview_markers import structural_match_lines


def _chunk(seq: int, body_md: str, *, blocks_text: str | None = None) -> FileChunk:
    return FileChunk(
        parent_id="p",
        path="/x.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="",
        chunk_seq=seq,
        blocks=[Block("p", body_md if blocks_text is None else blocks_text)],
        body_md=body_md,
    )


def test_match_after_a_long_chunk_lands_deep_not_mid() -> None:
    """A 120-line match-free chunk, then a 3-line chunk whose 2nd line
    matches: the marker must sit at line 121 of 123 (≈98% down), not at
    the 50% an ordinal mapping (chunk 1 of 2) would produce."""
    spec = MatchSpec.from_query("glimmer")
    long_body = "\n".join(["filler line"] * 120)
    short_body = "## Section\nmatch glimmer here\ndone"
    chunks = [_chunk(0, long_body), _chunk(1, short_body)]

    lines, total = structural_match_lines(chunks, spec)

    assert total == 123
    assert lines == [121]


def test_intra_chunk_offset_is_preserved() -> None:
    """The marker tracks which line *within* the chunk matched, not just
    the chunk boundary."""
    spec = MatchSpec.from_query("glimmer")
    chunks = [
        _chunk(0, "a\nb\nc\nd"),  # 4 lines, no match
        _chunk(1, "x\ny\nglimmer\nz"),  # match on local line 2 -> abs 6
    ]
    lines, total = structural_match_lines(chunks, spec)
    assert total == 8
    assert lines == [6]


def test_multiple_matched_chunks_sorted_and_deduped() -> None:
    spec = MatchSpec.from_query("glimmer")
    chunks = [
        _chunk(0, "glimmer\nfoo"),  # 2 lines, match at abs 0
        _chunk(1, "nothing here"),  # 1 line, no match
        _chunk(2, "bar\nglimmer"),  # starts at abs 3, match on local 1 -> abs 4
    ]
    lines, total = structural_match_lines(chunks, spec)
    assert total == 5
    assert lines == [0, 4]


def test_empty_query_clears_markers_but_keeps_track_length() -> None:
    spec = MatchSpec.from_query("")
    chunks = [_chunk(0, "a\nb\nc")]
    lines, total = structural_match_lines(chunks, spec)
    assert lines == []
    assert total == 3


def test_block_text_fallback_marks_chunk_top_when_serialisation_differs() -> None:
    """If the per-line scan of body_md misses the term but the block text
    carries it (a serialisation that phrased things differently), the
    chunk is still marked — at its top — so no matched chunk is silent."""
    spec = MatchSpec.from_query("glimmer")
    chunks = [
        _chunk(0, "plain\nlines\nonly", blocks_text="glimmer in blocks only"),
    ]
    lines, total = structural_match_lines(chunks, spec)
    assert total == 3
    assert lines == [0]
