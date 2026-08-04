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


def uses_markdown_renderer(c: PreviewBody) -> bool:
    """True when this chunk should mount through the structural Markdown
    renderer. A chunk needs both a markdown-capable kind AND non-empty
    ``body_md``; chunks failing either condition take the flat per-line
    path.

    Public so ``app.py``'s per-chunk mount loop, the file-level
    ``choose_preview_mode`` decision and ``match_evidence`` share one source
    of truth.
    """
    return c.kind in _MARKDOWN_RENDERED_KINDS and bool(c.body_md)


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
