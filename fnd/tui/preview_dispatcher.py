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

from typing import Literal

from fnd.query import FileChunk

# Kinds whose extractor emits a Markdown serialisation in ``body_md``;
# these go through the structural Markdown widget. PDFs are included
# only when the optional ``pdf-structure`` extra is installed — without
# it, ``body_md`` stays empty and the any-chunk predicate below keeps
# PDFs on the flat path automatically.
_MARKDOWN_RENDERED_KINDS: frozenset[str] = frozenset({"md", "docx", "pptx", "pdf"})

PreviewMode = Literal["flat", "structural"]


def choose_preview_mode(chunks: list[FileChunk]) -> PreviewMode:
    """Pick the preview path for ``chunks``.

    Returns ``"structural"`` when **any** chunk is markdown-rendered (a
    kind in :data:`_MARKDOWN_RENDERED_KINDS` *and* a non-empty
    ``body_md``); otherwise ``"flat"``. Empty ``chunks`` falls through
    to ``"flat"`` so the empty-state path doesn't try to mount the
    structural widget against zero blocks.

    The any-chunk semantics matches the legacy ``_uses_markdown_renderer``
    helper which decided per-chunk. Mixed files (a markdown file with a
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
        if c.kind in _MARKDOWN_RENDERED_KINDS and c.body_md:
            return "structural"
    return "flat"


__all__ = ["PreviewMode", "choose_preview_mode"]
