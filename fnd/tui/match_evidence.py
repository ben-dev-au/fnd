"""Will the user actually see a highlight where this result lands?

Search and preview read two different strings. ``F_BODY`` is what the index
matched; the preview paints ``body_md`` (structural renderer) or the
``body_struct`` block text (flat renderer). Where those diverge, a result can be
a genuine engine match with nothing to highlight where the user lands.

This module is the one place that answers "what text will the user read, and is
the match visible in it". Every surface that claims a match — the row marker,
the preview's landing signal, the scrollbar markers — reads from here, so they
cannot drift apart.

**It reports; it must never filter.** Nothing here may drop, hide or reorder a
result. The *engine's* match is what makes a result a result; if the highlighter
cannot paint that match, it is a defect to surface, not a reason to withhold the
row. Filtering on paintability would make the highlighter the gate, and a bug in
it would silently subtract matches the user never learns they missed — the
failure mode this module exists to make loud instead.

Some search/render divergences are deliberate and stay:

* markdown ``F_BODY`` is the raw inline source, so link URLs and inline HTML are
  searchable but never rendered — wanted, people search for domains;
* ODF slide and sheet names are synthesised labels, not document text.

An *accidental* divergence is an extractor bug, and belongs there — see
:class:`fnd.extract.heading_fold.HeadingFolder` (which folds a heading into
every representation or none) and ``FlatFallbackTier`` (which backfills prose
the structured parser dropped). Repairing one here, at render time, would pay
the cost on every navigation instead of once at index time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fnd.render import text_has_any_match
from fnd.tui.preview_dispatcher import PreviewBody, uses_markdown_renderer

if TYPE_CHECKING:
    from fnd.matching import MatchSpec

__all__ = ["has_paintable_match", "rendered_text"]


def rendered_text(body: PreviewBody) -> str:
    """The text the preview will actually paint for this chunk.

    Routes on :func:`fnd.tui.preview_dispatcher.uses_markdown_renderer` — the
    same predicate the mount loop uses — so this can't disagree with what gets
    mounted.
    """
    return body.body_md if uses_markdown_renderer(body) else body.body_text


def has_paintable_match(body: PreviewBody, spec: MatchSpec) -> bool:
    """True when the preview can show the user a highlight for this chunk.

    An empty spec (a filter-only query, or highlights toggled off) reports
    True: there is no match to locate, so there is nothing to warn about.
    """
    if spec.is_empty:
        return True
    return text_has_any_match(rendered_text(body), spec)
