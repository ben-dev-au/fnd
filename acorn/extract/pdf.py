"""PDF extractor: one chunk per page, with TOC-first heading detection.

Per plan §17 + §21 Spike B:

1. Try ``doc.get_toc()`` first — if the PDF has an embedded outline, that's
   ~100% accurate; map each page to the nearest preceding TOC entry.
2. Else fall back to ``pymupdf4llm.IdentifyHeaders`` font-size clustering.
3. Apply sanity gates and bail to ``heading_path = ""`` when:
   - ≤1 distinct rounded font size (likely OCR'd; clustering yields garbage)
   - >30% of spans flagged as headings (false positives dominate)
   - Page is slide-shaped (landscape ~10:7.5 + sparse text)

When heading_path can't be derived, the chunk still ranks via body/title/path
and the user navigates by page number.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pymupdf  # type: ignore[import-not-found]

from acorn.extract.base import Block, Chunk


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()


def _toc_heading_for_page(toc: list[list[object]], page_no_1based: int) -> str:
    """Walk the TOC and return ``A > B > C`` for the deepest entry whose page
    number is ``<= page_no_1based``. Empty if no entry applies."""
    stack: list[tuple[int, str]] = []  # (level, title)
    last_match_path: str = ""
    for entry in toc:
        # toc entries are [level, title, page, ...]; pymupdf returns object-typed.
        if not entry:
            continue
        level = int(cast(int, entry[0]))
        title = str(entry[1]) if len(entry) > 1 else ""
        page = int(cast(int, entry[2])) if len(entry) > 2 else 0
        if page == 0:
            continue
        if page > page_no_1based:
            break
        stack[level - 1 :] = [(level, title)]
        last_match_path = " > ".join(t for _, t in stack)
    return last_match_path


def _is_slide_shape(rect: pymupdf.Rect, span_count: int) -> bool:
    """Heuristic: landscape aspect ratio + few text spans."""
    w, h = float(rect.width), float(rect.height)
    if h == 0:
        return False
    ratio = w / h
    return ratio > 1.2 and span_count < 60


def _font_clustering_heading(
    page: pymupdf.Page,
) -> tuple[str, str]:
    """Return ``(heading_text_for_page, page_title)`` from font-size clustering.

    Bail (return ``("", "")``) when sanity gates trip. Title-on-slide handling
    falls out naturally because slides are detected upstream.
    """
    text_dict = cast(dict[str, Any], page.get_text("dict"))
    spans: list[tuple[float, str, int]] = []  # (rounded_size, text, flags)
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = (span.get("text") or "").strip()
                if not txt:
                    continue
                size = round(float(span.get("size", 0.0)))
                spans.append((float(size), txt, int(span.get("flags", 0))))

    if not spans:
        return ("", "")

    # Sanity gate 1: ≤1 distinct font size → likely OCR'd.
    distinct_sizes = {s for s, _, _ in spans}
    if len(distinct_sizes) <= 1:
        return ("", "")

    sizes_sorted = sorted(distinct_sizes, reverse=True)
    body_size = max(distinct_sizes, key=lambda s: sum(1 for x in spans if x[0] == s))

    candidates: list[str] = []
    for size, txt, _flags in spans:
        if size > body_size and len(txt) <= 120 and not txt.endswith((".", "!", "?")):
            candidates.append(txt)

    # Sanity gate 2: >30% of spans flagged as headings → too noisy.
    if len(candidates) > 0.30 * len(spans):
        return ("", "")

    # The single largest distinct size on the page is the page-level heading.
    largest = sizes_sorted[0]
    page_headings = [t for s, t, _ in spans if s == largest and len(t) <= 120]
    page_title = page_headings[0] if page_headings else ""
    heading_text = page_title
    return (heading_text, page_title)


def extract(path: Path) -> Iterator[Chunk]:
    parent_id = _parent_id(path)
    mtime = int(path.stat().st_mtime)

    doc = pymupdf.open(str(path))
    try:
        meta = doc.metadata or {}
        meta_title = str(meta.get("title") or "")
        meta_author = str(meta.get("author") or "")
        toc: list[list[object]] = doc.get_toc() or []

        for page_index in range(doc.page_count):
            page = doc[page_index]
            page_no = page_index + 1
            # Printed page label (e.g. "292", "iv") if the PDF carries
            # explicit labels; empty string otherwise. Books with
            # prefatory pages typically label them in roman numerals
            # so the displayed locator matches what's actually printed
            # on the page, while ``page_no`` (PDF index) is what Skim
            # needs for deep-linking.
            try:
                page_label = page.get_label() or ""
            except Exception:
                page_label = ""
            text = cast(str, page.get_text("text") or "")

            # Skip blank pages.
            if not text.strip():
                continue

            # Heading: TOC-first, then font clustering, then empty.
            heading_path = ""
            page_title = ""
            if toc:
                heading_path = _toc_heading_for_page(toc, page_no)
                page_title = heading_path.split(" > ")[-1] if heading_path else ""

            if not heading_path:
                # Slide-shape sanity gate before we even cluster.
                td = cast(dict[str, Any], page.get_text("dict"))
                span_count = sum(
                    1
                    for b in td.get("blocks", [])
                    for ln in b.get("lines", [])
                    for sp in ln.get("spans", [])
                    if (sp.get("text") or "").strip()
                )
                if not _is_slide_shape(page.rect, span_count):
                    h, t = _font_clustering_heading(page)
                    heading_path = h
                    page_title = t

            blocks: list[Block] = []
            if page_title:
                blocks.append(Block(kind="h2", text=page_title))
            blocks.append(Block(kind="p", text=text.strip()))

            yield Chunk(
                parent_id=parent_id,
                path=str(path),
                mtime=mtime,
                kind="pdf",
                body=text,
                body_struct=blocks,
                page=page_no,
                page_label=page_label,
                heading_path=heading_path,
                title=meta_title,
                author=meta_author,
                chunk_seq=page_index,
            )
    finally:
        doc.close()
