"""Flat (line-buffer) preview path.

``FlatBufferView`` owns the shared :class:`LineBufferPreview` widget,
the per-file rendered-document cache, and the install / activate /
reset lifecycle for the flat preview pipeline.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.widgets import Static

from fnd.tui.line_buffer import FileView, LineBufferPreview, build_file_view
from fnd.tui.preview.liveness import is_live
from fnd.tui.widgets.markdown import _build_match_spans
from fnd.tui.widgets.preview_container import PreviewContainer

if TYPE_CHECKING:
    from fnd.query import FileChunk
    from fnd.tui.app import FNDApp
    from fnd.tui.line_buffer import RenderedDocument

__all__ = ["FlatBufferView"]


class FlatBufferView:
    """Owns the shared flat-preview widget and its value cache; one
    instance lives on the app for the session."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        # Per-file flat-buffer value cache (Stage 1c). One shared
        # LineBufferPreview is mounted on first need and re-installed
        # via set_prebuilt_view for every (parent_id, query_sig)
        # activation. ``active_buffer`` is the shared widget when
        # flat is the visible preview, else None.
        self.cache: OrderedDict[tuple[str, str], RenderedDocument] = OrderedDict()
        self.active_buffer: LineBufferPreview | None = None
        self.shared_buffer: LineBufferPreview | None = None
        # (parent_id, query_sig) of whichever RenderedDocument is currently
        # installed in the shared widget. Lets intra-file navigation skip
        # set_prebuilt_view and just scroll.
        self.installed_key: tuple[str, str] | None = None

    def ensure_shared_buffer(self) -> LineBufferPreview | None:
        """Lazy-mount the single hidden LineBufferPreview under #preview_pane.

        Returns None when there is no pane to mount into — the screen is
        being torn down, or has not composed yet. This runs on the
        ``preview-load`` worker, so raising here surfaces as a WorkerFailed
        that takes the run down instead of a mount that quietly has nothing
        to do. Mirrors ``MatchNavigator._pane`` and the guarded measurement
        in ``presenter`` / ``prefetch``.
        """
        import contextlib

        buf = self.shared_buffer
        # ``is_live`` rather than ``parent is not None``: a buffer whose removal
        # is queued still reports a parent (Textual prunes on a message), so the
        # old test handed back a widget that was about to be torn out — the doc
        # was installed into it and the flat preview painted nothing. A dead
        # buffer is replaced, and ``installed_key`` reset so the fresh widget
        # gets the document installed rather than short-circuiting as "already
        # showing this file".
        if is_live(buf):
            return buf
        if buf is not None:
            self.installed_key = None
            self.active_buffer = None
        try:
            pane = self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return None
        for w in list(pane.children):
            if isinstance(w, Static) and w.id == "placeholder":
                with contextlib.suppress(Exception):
                    w.remove()
        buf = LineBufferPreview(
            wrap=True, show_match_markers=self._app._preview.scrollbar_markers_enabled
        )
        buf.add_class("-hidden")
        pane.mount(buf)
        self.shared_buffer = buf
        return buf

    def install_doc(
        self,
        buf: LineBufferPreview,
        doc: RenderedDocument,
        focus_chunk_seq: int,
        *,
        parent_id: str,
        context_fraction: float = 0.0,
    ) -> None:
        """Install ``doc`` into ``buf`` scrolled to the focused chunk's match."""
        focus_line = self.focus_line_for_chunk(doc.fv, focus_chunk_seq)
        buf.set_prebuilt_view(
            doc.fv,
            doc.strips,
            doc.visual_to_logical,
            doc.logical_to_visual_start,
            wrap_width=doc.wrap_width,
            base_width=doc.base_width,
            initial_focus_line=focus_line,
            context_fraction=context_fraction,
        )
        buf.parent_doc_id = parent_id  # type: ignore[attr-defined]

    def reset(self) -> None:
        """Hide + clear the shared widget when the value cache is invalidated."""
        import contextlib

        self.active_buffer = None
        self.installed_key = None
        buf = self.shared_buffer
        if buf is None:
            return
        with contextlib.suppress(Exception):
            buf.add_class("-hidden")
        with contextlib.suppress(Exception):
            buf.clear()

    @staticmethod
    def focus_line_for_chunk(fv: FileView, chunk_id: int) -> int | None:
        """First matched line in ``chunk_id``, falling back to chunk start.
        Mirrors LineBufferPreview.scroll_to_chunk so the synchronous
        pre-paint scroll lands at the same place the deferred call did."""
        target = fv.first_hit_line_in_chunk.get(chunk_id)
        if target is None:
            rng = fv.chunk_to_range.get(chunk_id)
            if rng is not None:
                target = rng[0]
        return target

    def build_file_view(self, chunks: list[FileChunk]) -> FileView:
        """Convert decoded chunks into a :class:`FileView` for the flat
        path. Reuses the same word-level match-span helper the
        structural renderer uses so highlight semantics — including
        the per-word colour (yellow for exact matches, orange for
        fuzzy ones) — agree across pipelines."""
        spec = self._app._effective_match_spec
        import os

        if (
            os.environ.get("_FND_FLAT_MD_STYLED") == "1"
            and chunks
            and any(c.kind == "md" and c.body_md for c in chunks)
        ):
            from fnd.tui._md_flat import build_md_file_view

            try:
                pane_widget = self._app.query_one("#preview_pane", VerticalScroll)
                wrap_width = max(20, pane_widget.content_size.width - 1)
            except Exception:
                wrap_width = 80
            return build_md_file_view(chunks, spec=spec, wrap_width=wrap_width)
        triples: list[tuple[int, str, list[tuple[int, int] | tuple[int, int, str]]]] = []
        for c in chunks:
            body_text = "\n".join(b.text for b in c.blocks)
            spans = _build_match_spans(body_text, spec) if not spec.is_empty else []
            styled_spans: list[tuple[int, int] | tuple[int, int, str]] = [
                (s.start, s.end, str(s.style)) for s in spans
            ]
            triples.append((c.chunk_seq, body_text, styled_spans))
        return build_file_view(triples)

    def activate(self, buf: LineBufferPreview) -> None:
        """Show ``buf`` and hide every other preview widget (structural
        containers and other flat buffers) so only one file is on
        screen at a time."""
        from fnd.tui import _perf

        self._app._preview.clear_pane_placeholder()
        for child in self._app.query(PreviewContainer):
            child.add_class("-hidden")
        for child in self._app.query(LineBufferPreview):
            if child is buf:
                child.remove_class("-hidden")
            else:
                child.add_class("-hidden")
        self.active_buffer = buf
        self._app._preview.active = None
        # Reset the structural-path alias dicts so any straggler scroll
        # call can't accidentally try to scroll to a now-orphaned widget.
        self._app._preview.chunk_widgets = {}
        self._app._preview.match_targets = {}
        _perf.mark(
            "click_to_display_end",
            parent_id=getattr(buf, "parent_doc_id", None),
            path="flat_activate",
        )
