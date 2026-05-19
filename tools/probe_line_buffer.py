"""Phase 2 visual probe — load the real stress-test PDF into LineBufferPreview.

Run from the repo root:

    uv run python tools/probe_line_buffer.py

Or against a different PDF / collection / query:

    uv run python tools/probe_line_buffer.py \
        --collection cpl --query "AWS" \
        --pdf "/path/to/some.pdf"

What it proves (before FNDApp depends on the widget):

* The vacuum FileView builds correctly from real Tantivy chunks (not just
  synthetic test fixtures).
* ``scroll_to_chunk(first_hit)`` lands on the matched line, not the
  chunk top.
* ``set_focused_chunk(...)`` repaints only the affected slice — no full
  buffer flicker.
* Multi-line drag-select works in a real terminal (``ALLOW_SELECT``).
* Glyph widths and non-ASCII characters (™ ® em-dashes) render at the
  expected cell widths so horizontal scroll sizes itself correctly.

Keys:
    q   quit
    f   cycle focused chunk between known matched chunks, then clear
    m   jump to next match line
    M   jump to prev match line
    g   jump back to the first match
    Arrows / PgUp / PgDn / Home / End — default ScrollView bindings

Not a pytest target — interactive probe only. No production code is
imported except the bits the host app would call.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import time
from pathlib import Path
from typing import ClassVar

# Allow running from the repo root via ``python tools/probe_line_buffer.py``.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import BindingType  # noqa: E402
from textual.widgets import Footer, Static  # noqa: E402

from fnd.config import default_index_dir  # noqa: E402
from fnd.matching import MatchSpec, word_matches  # noqa: E402
from fnd.query import Searcher  # noqa: E402
from fnd.tui.line_buffer import LineBufferPreview, build_file_view  # noqa: E402

DEFAULT_PDF_PATH = (
    "/Users/BenDavidson/Documents/Uni/B. Software Engineering (Honours)/"
    "2026 Semester 1/Cloud Platforms/9 - Resources/"
    "26S1CPL - wellarchitected-framework.pdf"
)
DEFAULT_COLLECTION = "cpl"
DEFAULT_QUERY = "AWS"


def _chunk_match_spans(text: str, spec: MatchSpec) -> list[tuple[int, int]]:
    """Word-level match span detection for the probe.

    Mirrors ``fnd.tui.app._build_match_spans`` byte-offset-wise: every
    ``\\w+`` token that matches the spec (exact-stem or fuzzy-AUTO)
    contributes one ``(start, end)`` byte range. Returned in source
    order so ``build_file_view`` can clip per line without resorting.
    """
    if spec.is_empty or not text:
        return []
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"\w+", text):
        if word_matches(m.group(0), spec):
            spans.append((m.start(), m.end()))
    return spans


class _Probe(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #status {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }
    LineBufferPreview {
        height: 1fr;
        border: round $primary;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        ("q", "quit", "Quit"),
        ("f", "toggle_focus", "Cycle focus"),
        ("m", "next_match", "Next match"),
        ("shift+m", "prev_match", "Prev match"),
        ("g", "first_match", "First match"),
    ]

    def __init__(
        self,
        *,
        index_dir: Path,
        collection: str,
        query: str,
        pdf_path: str,
    ) -> None:
        super().__init__()
        self._index_dir = index_dir
        self._collection = collection
        self._query = query
        self._pdf_path = pdf_path
        self._status_msg = "loading…"
        # Two known matched chunks the 'f' key cycles between (+ a clear step).
        self._focus_cycle: list[int | None] = []
        self._focus_idx: int = -1
        self._match_cursor: int = -1
        self._first_match_chunk: int | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._status_msg, id="status")
        yield LineBufferPreview(id="buf")
        yield Footer()

    async def on_mount(self) -> None:
        # Decode + build on the UI thread for this one-off probe. Parallel
        # decode lives in Phase 4; here we want raw wall-clock numbers.
        self.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        try:
            t0 = time.perf_counter()
            searcher = Searcher(index_dir=self._index_dir)
            t_open = time.perf_counter()
            spec = MatchSpec.from_query(self._query)
            hits = searcher.search(self._query, collection=self._collection, limit=200)
            t_search = time.perf_counter()
            target = Path(self._pdf_path).resolve()
            target_hit = next((h for h in hits if Path(h.path).resolve() == target), None)
            if target_hit is None:
                names = ", ".join(sorted({Path(h.path).name for h in hits})[:5]) or "(none)"
                self._set_status(
                    f"no hit for '{target.name}' in '{self._collection}' for query "
                    f"'{self._query}'. top hits: {names}"
                )
                return
            parent_id = target_hit.parent_id
            chunks = searcher.get_file_chunks(parent_id)
            # Defensive sort — Searcher.get_file_chunks doesn't promise order.
            chunks.sort(key=lambda c: c.chunk_seq)
            t_chunks = time.perf_counter()
            triples: list[tuple[int, str, list[tuple[int, int]]]] = []
            for ch in chunks:
                body_text = "\n".join(b.text for b in ch.blocks)
                triples.append((ch.chunk_seq, body_text, _chunk_match_spans(body_text, spec)))
            t_spans = time.perf_counter()
            fv = build_file_view(triples)
            t_build = time.perf_counter()

            buf = self.query_one(LineBufferPreview)
            buf.set_file_view(fv)
            t_mount = time.perf_counter()

            if fv.first_hit_line_in_chunk:
                first = next(iter(fv.first_hit_line_in_chunk))
                self._first_match_chunk = first
                buf.scroll_to_chunk(first, prefer_first_match=True)
                matched_chunks = list(fv.first_hit_line_in_chunk)
                # Pick two distant matched chunks if possible.
                if len(matched_chunks) >= 2:
                    self._focus_cycle = [matched_chunks[0], matched_chunks[-1], None]
                else:
                    self._focus_cycle = [matched_chunks[0], None]
            self._focus_idx = -1

            self._set_status(
                f"chunks={len(chunks)} lines={fv.line_count} matches={len(fv.match_lines)} "
                f"| open {(t_open - t0) * 1000:.0f}ms "
                f"search {(t_search - t_open) * 1000:.0f}ms "
                f"decode {(t_chunks - t_search) * 1000:.0f}ms "
                f"spans {(t_spans - t_chunks) * 1000:.0f}ms "
                f"build {(t_build - t_spans) * 1000:.0f}ms "
                f"mount {(t_mount - t_build) * 1000:.0f}ms"
            )
        except Exception as exc:
            self._set_status(f"ERROR: {type(exc).__name__}: {exc}")

    def _set_status(self, msg: str) -> None:
        self._status_msg = msg
        with contextlib.suppress(Exception):
            self.query_one("#status", Static).update(msg)

    def action_toggle_focus(self) -> None:
        if not self._focus_cycle:
            return
        buf = self.query_one(LineBufferPreview)
        self._focus_idx = (self._focus_idx + 1) % len(self._focus_cycle)
        target = self._focus_cycle[self._focus_idx]
        buf.set_focused_chunk(target)
        if target is not None:
            buf.scroll_to_chunk(target, prefer_first_match=True, center=True)
            self._set_status(f"focused chunk={target}")
        else:
            self._set_status("focus cleared")

    def action_next_match(self) -> None:
        buf = self.query_one(LineBufferPreview)
        lines = buf.match_lines
        if not lines:
            return
        self._match_cursor = (self._match_cursor + 1) % len(lines)
        buf.scroll_to_line(lines[self._match_cursor], center=True)
        self._set_status(
            f"match {self._match_cursor + 1}/{len(lines)} @ line {lines[self._match_cursor]}"
        )

    def action_prev_match(self) -> None:
        buf = self.query_one(LineBufferPreview)
        lines = buf.match_lines
        if not lines:
            return
        self._match_cursor = (self._match_cursor - 1) % len(lines)
        buf.scroll_to_line(lines[self._match_cursor], center=True)
        self._set_status(
            f"match {self._match_cursor + 1}/{len(lines)} @ line {lines[self._match_cursor]}"
        )

    def action_first_match(self) -> None:
        if self._first_match_chunk is None:
            return
        buf = self.query_one(LineBufferPreview)
        buf.scroll_to_chunk(self._first_match_chunk, prefer_first_match=True)
        self._match_cursor = -1
        self._set_status(f"first matched chunk={self._first_match_chunk}")


def _resolve_index_dir() -> Path:
    override = os.environ.get("_FND_INDEX_DIR")
    if override:
        return Path(override).expanduser()
    return default_index_dir()


def main() -> None:
    p = argparse.ArgumentParser(description="Real-PDF visual probe for LineBufferPreview")
    p.add_argument("--query", default=DEFAULT_QUERY)
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--pdf", default=DEFAULT_PDF_PATH)
    args = p.parse_args()

    index_dir = _resolve_index_dir()
    if not index_dir.exists():
        print(f"index_dir not found: {index_dir}", file=sys.stderr)
        sys.exit(1)
    if not Path(args.pdf).exists():
        print(f"pdf not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    app = _Probe(
        index_dir=index_dir,
        collection=args.collection,
        query=args.query,
        pdf_path=args.pdf,
    )
    app.run()


if __name__ == "__main__":
    main()
