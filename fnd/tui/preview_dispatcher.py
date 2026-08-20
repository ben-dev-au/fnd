"""Routing decision for the preview pane.

The redesign keeps two parallel renderers:

* **Flat buffer** (:class:`fnd.tui.line_buffer.LineBufferPreview`) —
  one ScrollView per file, Strips cached per visual line. Used for
  ``.pdf`` and ``.txt`` files where there's no structure to render
  beyond plain text + match highlights. The win is that the post-load
  DOM is O(1) widgets per file regardless of file size.
* **Structural** (per-block Markdown widget tree) — used for ``.md``,
  ``.docx``, and ``.pptx``. These files index a markdown serialisation
  of their structure; the existing pipeline renders that through
  Textual's Markdown widget which honours headings, lists, fences,
  tables, etc. A flat-buffer line view would regress those.

This module is the single source of truth for the routing decision so
the host (``app.py``) and tests don't drift on what "uses the
structural renderer" means.
"""

from __future__ import annotations

from typing import Literal, Protocol

from fnd.kinds import MARKDOWN_RENDERED_KINDS
from fnd.query import FileChunk


class PreviewBody(Protocol):
    """The three fields the substrate decision needs from one chunk.

    Both :class:`fnd.query.FileChunk` (what the preview mounts) and
    :class:`fnd.query.Hit` (what the results pane lists) satisfy it, so a
    caller holding either can ask which text the user will end up reading —
    see :mod:`fnd.tui.match_evidence`. Read-only properties, since both
    implementations are frozen dataclasses.
    """

    @property
    def kind(self) -> str: ...
    @property
    def body_md(self) -> str: ...
    @property
    def body_text(self) -> str: ...


# Kinds whose extractor can emit a Markdown serialisation in ``body_md``;
# these route through the structural Markdown widget when the chunk actually
# has ``body_md`` populated (see the ``bool(c.body_md)`` guard below). Derived
# from the central registry — PDF is included because the optional
# ``pdf-structure`` extra populates ``body_md`` on PDF chunks; without it
# ``body_md`` stays empty and the predicate keeps PDFs on the flat path.
_MARKDOWN_RENDERED_KINDS: frozenset[str] = MARKDOWN_RENDERED_KINDS

PreviewMode = Literal["flat", "structural"]


# Largest chunk, in markdown characters, that the structural renderer will build.
# Measured on a real 727-chunk PDF: a 120,123-character chunk took 4,424ms to
# build into 7,184 rendered rows, against a 5.3ms median — a multi-second freeze,
# observed live at 8.4s, because Textual builds the widget on the event loop.
# Every ordinary chunk in that file was under 6,000 characters, so this sits an
# order of magnitude clear of normal content.
#
# The flat path this routes to is CHEAPER, not cheap. It emits one Static per
# non-empty source line for a match-bearing chunk (the single-piece fast path
# applies only where there is no match, and the chunk you navigate to has one),
# and mounting those is superlinear. Measured head-to-head on a synthetic
# 120,036-character table, 1,596 lines: flat 224ms to render plus 725ms to mount
# and settle, against 979ms structural — near parity at that shape. The win on
# the real chunk comes from its structural cost being far above linear (4,424ms),
# not from the flat path being fast. Do not raise this cap expecting the flat
# path to absorb it for free.
#
# Lives here rather than in preview.tuning because this module is what every
# path asks and it sits BELOW the preview package — importing tuning from here
# is an import cycle.
MARKDOWN_MAX_CHARS = 40_000


def uses_markdown_renderer(c: PreviewBody) -> bool:
    """True when this chunk should mount through the structural Markdown
    renderer. A chunk needs both a markdown-capable kind AND non-empty
    ``body_md``; chunks failing either condition take the flat per-line
    path.

    Public so ``app.py``'s per-chunk mount loop, the file-level
    ``choose_preview_mode`` decision and ``match_evidence`` share one source
    of truth.
    """
    if c.kind not in _MARKDOWN_RENDERED_KINDS or not c.body_md:
        return False
    # And it must be small enough to BUILD — see MARKDOWN_MAX_CHARS above for
    # the measurements and for what the flat path over the cap actually costs.
    # What is lost over the cap is the table structure of something that was
    # never going to be usable as a 7,184-row widget anyway. Gating here rather
    # than in the mount is deliberate: this is the one function the mount, the
    # background warmer and match_evidence all ask, so they cannot disagree.
    return len(c.body_md) <= MARKDOWN_MAX_CHARS


def choose_preview_mode(chunks: list[FileChunk]) -> PreviewMode:
    """Pick the preview path for ``chunks``.

    Returns ``"structural"`` when **any** chunk is markdown-rendered (a
    kind in :data:`_MARKDOWN_RENDERED_KINDS` *and* a non-empty
    ``body_md``); otherwise ``"flat"``. Empty ``chunks`` falls through
    to ``"flat"`` so the empty-state path doesn't try to mount the
    structural widget against zero blocks.

    The any-chunk semantics matches :func:`uses_markdown_renderer`
    which decides per-chunk. Mixed files (a markdown file with a
    handful of stale-body chunks) keep their structural path; pure
    text/PDF files take the flat path.
    """
    import os

    force = os.environ.get("_FND_FORCE_FLAT")
    if force == "1":
        return "flat"
    if force == "pdf" and chunks and all(c.kind == "pdf" for c in chunks):
        return "flat"
    for c in chunks:
        if uses_markdown_renderer(c):
            return "structural"
    return "flat"


__all__ = ["PreviewBody", "PreviewMode", "choose_preview_mode", "uses_markdown_renderer"]
