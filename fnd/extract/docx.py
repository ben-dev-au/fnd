"""DOCX extractor: one chunk per heading section.

Walks the body in document order — interleaving paragraphs and tables —
and accumulates body until a Heading 1/2/3 paragraph appears, at which
point it flushes the current section as a chunk and starts a new one.
Tracks an ancestor stack so each chunk ships its full ``heading_path``
like ``Methods Document > Sampling``.

Each chunk also carries the section's content as markdown source on
``body_md`` for the structural preview renderer (Textual Markdown
widget). Bold / italic runs become ``**…**`` / ``*…*``; bulleted and
numbered lists detected via either paragraph style or the underlying
``numPr`` element become GFM list lines; tables become GFM pipe
tables. ``body_struct`` keeps the legacy plain-text Block list because
the snippet pipeline reads from there and snippets shouldn't show
markdown markers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from fnd.extract._ooxml import reject_if_zip_bomb
from fnd.extract.base import Block, Chunk, ExtractError

_HEADING_LEVELS: dict[str, int] = {
    "Heading 1": 1,
    "Heading 2": 2,
    "Heading 3": 3,
    "Heading 4": 4,
    "Heading 5": 5,
    "Heading 6": 6,
    "Title": 1,
}


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def _heading_level(p: Paragraph) -> int:
    style = p.style
    name = getattr(style, "name", "") if style is not None else ""
    return _HEADING_LEVELS.get(name, 0)


def _paragraph_md(para: Paragraph) -> str:
    """Render one paragraph's runs as a markdown line.

    Bold runs wrap in ``**…**``; italic in ``*…*``; bold+italic in
    ``***…***``. Empty / whitespace-only runs are passed through
    unwrapped so a styled run that's just trailing whitespace doesn't
    produce stray empty markers like ``** **``.
    """
    parts: list[str] = []
    for run in para.runs:
        text = run.text
        if not text:
            continue
        bold = bool(run.bold)
        italic = bool(run.italic)
        if not text.strip():
            # Style markers around pure whitespace produce ugly artifacts
            # (`** **`, `* *`); pass whitespace through bare.
            parts.append(text)
            continue
        if bold and italic:
            parts.append(f"***{text}***")
        elif bold:
            parts.append(f"**{text}**")
        elif italic:
            parts.append(f"*{text}*")
        else:
            parts.append(text)
    return "".join(parts)


def _list_info(para: Paragraph) -> tuple[str, int] | None:
    """Detect whether ``para`` is a list item; return ``(prefix, depth)``
    or ``None``.

    Three signal sources are consulted (in priority order):

    1. The paragraph style name — ``List Bullet``, ``List Bullet 2``…
       map to bullet at increasing depths; ``List Number`` etc. map to
       numbered.
    2. The underlying ``w:pPr/w:numPr`` element (XML) for any paragraph
       whose style doesn't carry list semantics but whose author
       attached a numbering definition directly. ``ilvl`` becomes the
       depth; we default to bullet since resolving the abstract num to
       distinguish bulleted vs numbered requires walking ``numbering.xml``
       and is a known follow-up.
    """
    style = para.style
    name = getattr(style, "name", "") if style is not None else ""
    if name.startswith("List Bullet"):
        # "List Bullet" depth 0, "List Bullet 2" depth 1, etc.
        suffix = name.removeprefix("List Bullet").strip()
        depth = max(int(suffix) - 1, 0) if suffix.isdigit() else 0
        return ("- ", depth)
    if name.startswith("List Number") or name.startswith("List Continue"):
        suffix = name.removeprefix("List Number").removeprefix("List Continue").strip()
        depth = max(int(suffix) - 1, 0) if suffix.isdigit() else 0
        return ("1. ", depth)
    if name in {"List Paragraph"}:
        # Word's generic "List Paragraph" style: ilvl tells us depth,
        # numId presence tells us it's actually a list.
        ppr = para._element.find(qn("w:pPr"))
        if ppr is None:
            return None
        numpr = ppr.find(qn("w:numPr"))
        if numpr is None:
            return None
        ilvl = numpr.find(qn("w:ilvl"))
        depth = int(ilvl.get(qn("w:val"))) if ilvl is not None else 0
        return ("- ", depth)
    ppr = para._element.find(qn("w:pPr"))
    if ppr is not None:
        numpr = ppr.find(qn("w:numPr"))
        if numpr is not None:
            ilvl = numpr.find(qn("w:ilvl"))
            depth = int(ilvl.get(qn("w:val"))) if ilvl is not None else 0
            return ("- ", depth)
    return None


def _table_md(table: Table) -> str:
    """Serialise a docx table to a GFM pipe table.

    First row → header. The separator row is required by GFM. Cell text
    is each cell's plain text (newlines collapsed to spaces so the
    pipe layout stays single-line); pipe characters inside a cell are
    backslash-escaped so they don't break column alignment.
    """
    rows = list(table.rows)
    if not rows:
        return ""

    def _cell_text(cell: Any) -> str:
        text = cell.text or ""
        # Collapse internal newlines to spaces; escape pipes so they
        # don't break the table column layout.
        return text.replace("\n", " ").replace("|", r"\|").strip()

    header_cells = [_cell_text(c) for c in rows[0].cells]
    width = len(header_cells)
    header_line = "| " + " | ".join(header_cells) + " |"
    sep_line = "|" + "|".join(["------"] * width) + "|"
    body_lines = []
    for row in rows[1:]:
        cells = [_cell_text(c) for c in row.cells]
        # Pad short rows so column count stays consistent.
        while len(cells) < width:
            cells.append("")
        body_lines.append("| " + " | ".join(cells[:width]) + " |")
    return "\n".join([header_line, sep_line, *body_lines])


def _flush(
    *,
    path: Path,
    parent_id: str,
    mtime: int,
    heading_stack: list[str],
    blocks: list[Block],
    body_parts: list[str],
    md_lines: list[str],
    seq: int,
    deck_title: str,
) -> Chunk | None:
    body = "\n".join(b for b in body_parts if b).strip()
    if not body and not heading_stack:
        return None
    heading_path = " > ".join(heading_stack)
    body_md = "\n\n".join(line for line in md_lines if line).rstrip()
    return Chunk(
        parent_id=parent_id,
        path=str(path),
        mtime=mtime,
        kind="docx",
        body=body,
        body_struct=blocks.copy(),
        body_md=body_md,
        heading_path=heading_path,
        title=deck_title,
        chunk_seq=seq,
    )


def extract(path: Path) -> Iterator[Chunk]:
    reject_if_zip_bomb(path)
    parent_id = _parent_id(path)
    yield from _extract_inner(path, parent_id)


def _extract_inner(path: Path, parent_id: str) -> Iterator[Chunk]:
    """Generator body for :func:`extract`, factored out so the whole
    iteration sits inside a single try/except. Without this split, a
    parser crash during ``doc.iter_inner_content()`` would propagate
    raw and abort the index build."""
    try:
        mtime = int(path.stat().st_mtime)
        try:
            doc = Document(str(path))
        except PackageNotFoundError as e:
            # `python-docx` raises this for encrypted / password-protected
            # packages too — we can't reliably tell them apart, so the
            # umbrella message is the right shape.
            raise ExtractError(str(path), f"unreadable docx: {e}") from e
        yield from _walk_docx_body(path=path, parent_id=parent_id, mtime=mtime, doc=doc)
    except ExtractError:
        raise
    except Exception as e:
        # Parser libs raise everything from RuntimeError to opaque C
        # extension errors. Broad except is intentional — the goal is
        # "don't let one bad doc stop the index build."
        raise ExtractError(str(path), f"{type(e).__name__}: {e}") from e


def _walk_docx_body(*, path: Path, parent_id: str, mtime: int, doc: Any) -> Iterator[Chunk]:
    heading_stack: list[str] = []
    blocks: list[Block] = []
    body_parts: list[str] = []
    md_lines: list[str] = []
    seq = 0
    doc_title = ""

    # Walk paragraphs and tables in document order so a table sandwiched
    # between two paragraphs renders in its real position.
    for item in doc.iter_inner_content():
        if isinstance(item, Paragraph):
            text = (item.text or "").strip()
            if not text:
                # Empty paragraphs serve as visual spacing in Word but
                # add nothing to the indexed body or rendered preview.
                continue
            level = _heading_level(item)
            if level > 0:
                # Flush the previous section before starting a new one.
                chunk = _flush(
                    path=path,
                    parent_id=parent_id,
                    mtime=mtime,
                    heading_stack=heading_stack,
                    blocks=blocks,
                    body_parts=body_parts,
                    md_lines=md_lines,
                    seq=seq,
                    deck_title=doc_title,
                )
                if chunk is not None:
                    yield chunk
                    seq += 1
                blocks = []
                body_parts = []
                md_lines = []
                heading_stack[level - 1 :] = [text]
                if not doc_title and level == 1:
                    doc_title = text
                blocks.append(Block(kind=f"h{level}", text=text))
                # Chunk's own heading goes into body_parts so F_BODY
                # reflects visible content; ancestors live in heading_path.
                body_parts.append(text)
                # The heading itself opens the new section's body_md.
                md_lines.append(f"{'#' * level} {text}")
                continue

            # Body paragraph: list or normal.
            list_info = _list_info(item)
            line_md = _paragraph_md(item)
            if list_info is not None:
                prefix, depth = list_info
                indent = "  " * depth
                md_lines.append(f"{indent}{prefix}{line_md}")
            else:
                md_lines.append(line_md)
            blocks.append(Block(kind="p", text=text))
            body_parts.append(text)
        else:
            assert isinstance(item, Table)  # iter_inner_content yields P | Table
            table_md = _table_md(item)
            if table_md:
                md_lines.append(table_md)
                # Index every cell value via body_parts so search hits
                # find table content. body_struct stays as plain ``p``
                # blocks (one per cell line) so snippet generation
                # surfaces the matched cell.
                for row in item.rows:
                    for cell in row.cells:
                        cell_text = (cell.text or "").strip()
                        if cell_text:
                            blocks.append(Block(kind="p", text=cell_text))
                            body_parts.append(cell_text)

    # Final flush.
    chunk = _flush(
        path=path,
        parent_id=parent_id,
        mtime=mtime,
        heading_stack=heading_stack,
        blocks=blocks,
        body_parts=body_parts,
        md_lines=md_lines,
        seq=seq,
        deck_title=doc_title,
    )
    if chunk is not None:
        yield chunk
