"""A section row at a normal pane width showed only its locator, snippet gone.

The budget elision kept the locator and dropped the whole snippet whenever
``locator + 80-char snippet`` did not fit, which at any real split-pane width
is always. `p.57` sat alone on a half-empty line while the preview held the
context the row was meant to show. The locator must survive, but the space
after it is the snippet's.
"""

from __future__ import annotations

from fnd.query import Hit
from fnd.tui.results_labels import _format_hit_label


def _hit(snippet: str, *, page: int = 57) -> Hit:
    return Hit(
        score=21.52,
        parent_id="p",
        path="/uda/Workshop4.pdf",
        kind="pdf",
        page=page,
        slide=0,
        heading_path="",
        title="",
        snippet=snippet,
        page_label=str(page),
    )


# A short page locator plus an 80-char snippet, at a budget that fits the
# locator with room to spare but not the whole snippet: a normal split pane.
_SNIPPET = "Word Count by tag, total views per question, then reduce and reflect on the sum"
_MID_BUDGET = 40


def test_a_short_locator_leaves_the_rest_for_the_snippet() -> None:
    plain = _format_hit_label(_hit(_SNIPPET), max_score=28.0, body_budget=_MID_BUDGET).plain

    assert "p.57" in plain, plain
    # Some of the snippet reaches the row; the line is not just the locator.
    assert "Word Count" in plain, plain


def test_the_row_stays_within_its_budget() -> None:
    """The control: keeping the snippet must not overrun the budget and clip
    against the border."""
    from rich.cells import cell_len

    from fnd.tui.results_labels import _shorten

    plain = _format_hit_label(_hit(_SNIPPET), max_score=28.0, body_budget=_MID_BUDGET).plain
    # The label carries a fixed score column then the body; the body is what the
    # budget bounds. Strip the leading score field before measuring.
    body = plain.split("  ", 1)[-1] if "  " in plain else plain
    assert cell_len(body) <= _MID_BUDGET, (cell_len(body), body)
    # And the snippet was genuinely shortened, not shown whole.
    assert _shorten(_SNIPPET, 80) not in plain, "the full snippet should not fit at this width"


def test_a_locator_too_wide_for_the_budget_still_elides_to_the_locator() -> None:
    """The other control: when even the locator overruns, keep its tail so
    sibling rows stay distinct (the behaviour the budget was added for)."""
    hit = Hit(
        score=21.52,
        parent_id="p",
        path="/uda/notes.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="A very long section heading that alone exceeds the tiny budget",
        title="",
        snippet=_SNIPPET,
        page_label="",
    )

    plain = _format_hit_label(hit, max_score=28.0, body_budget=12).plain

    assert "…" in plain, plain
    assert "Word Count" not in plain, "no room for a snippet when the locator itself overruns"
