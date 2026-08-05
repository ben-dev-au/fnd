"""The unlocatable signal has to actually reach the screen.

Both halves of it were emitted correctly and shown to nobody, and only driving
the real TUI in a terminal exposed either one:

* the row glyph was appended AFTER an 80-char snippet, so the results pane
  truncated it at the border on every row that had one;
* the preview's border notice was refreshed only when the ▲/▼ counts changed
  or the document re-mounted. Stepping between section rows of the same file
  does neither — and a chunk with no match doesn't scroll — so the border kept
  showing the previous row's state.

These tests pin the reachability, not the computation (which
test_match_evidence covers).
"""

from __future__ import annotations

from fnd.query import Hit
from fnd.tui.results_labels import _UNLOCATABLE_GLYPH, _format_hit_label

# Narrower than any real results pane, and far narrower than a row label.
NARROW_PANE = 40


def _hit(snippet: str, *, page: int = 27) -> Hit:
    return Hit(
        score=25.58,
        parent_id="p",
        path="/books/aws.pdf",
        kind="pdf",
        page=page,
        slide=0,
        heading_path="Introduction > Assessment Test",
        title="AWS Study Guide",
        snippet=snippet,
        page_label=str(page),
    )


def test_glyph_survives_a_pane_narrower_than_the_row() -> None:
    """A results pane is routinely narrower than locator + 80-char snippet, so
    a trailing marker is clipped and the user never sees it."""
    long_snippet = "Assessment Test C. Cryptographic transformation D. Encryption at rest 11. What"
    label = str(_format_hit_label(_hit(long_snippet), max_score=28.0, match_visible=False))

    assert _UNLOCATABLE_GLYPH in label[:NARROW_PANE], (
        f"glyph must appear within the first {NARROW_PANE} cells; got {label[:NARROW_PANE]!r}"
    )


def test_no_glyph_when_the_match_is_visible() -> None:
    label = str(_format_hit_label(_hit("plenty of test matches here"), max_score=28.0))

    assert _UNLOCATABLE_GLYPH not in label


def test_glyph_precedes_the_locator_so_rows_stay_alignable() -> None:
    label = str(_format_hit_label(_hit("some snippet"), max_score=28.0, match_visible=False))

    assert label.index(_UNLOCATABLE_GLYPH) < label.index("p.27")
