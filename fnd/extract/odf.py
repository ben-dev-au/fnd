"""OpenDocument extractor: .odt (text) · .odp (presentation) · .ods (spreadsheet).

ODF files are ZIPs whose ``content.xml`` holds the body. We parse it with
stdlib ``zipfile`` + ``lxml.etree`` (no odfpy) and, per subtype:

* ``.odt`` — walk ``office:text`` into the shared ``HeadingSectioner`` (headings
  from ``text:h``/``text:outline-level``, paragraphs, lists, tables), the same
  structural shape as docx.
* ``.odp`` — one chunk per ``draw:page`` slide, like pptx.
* ``.ods`` — one chunk per ``table:table`` sheet rendered as a GFM table,
  honouring ``number-columns/rows-repeated`` so repeated empty cells don't
  explode into huge phantom ranges.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Iterator
from pathlib import Path

from lxml import etree

from fnd.extract._ooxml import reject_if_zip_bomb
from fnd.extract._sectioner import HeadingSectioner
from fnd.extract._tables import gfm_table
from fnd.extract._xml import parse_xml
from fnd.extract.base import Block, Chunk, ExtractError
from fnd.fsmeta import FileTimes, read_file_times
from fnd.kinds import kind_for_suffix

_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"

# Caps so a spreadsheet's repeat-padded phantom range can't blow up the index.
_MAX_COLS = 64
_MAX_ROWS = 2000


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def _lname(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text_of(el: etree._Element) -> str:
    return " ".join("".join(str(t) for t in el.itertext()).split())


def _find_local(root: etree._Element, name: str) -> etree._Element | None:
    for el in root.iter():
        if _lname(el.tag) == name:
            return el
    return None


def extract(path: Path) -> Iterator[Chunk]:
    try:
        yield from _extract_inner(path)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(str(path), f"{type(e).__name__}: {e}") from e


def _extract_inner(path: Path) -> Iterator[Chunk]:
    reject_if_zip_bomb(path)
    kind = kind_for_suffix(path.suffix)
    if kind is None:
        return
    times = read_file_times(path)
    parent_id = _parent_id(path)

    with zipfile.ZipFile(path) as zf:
        try:
            content = parse_xml(zf.read("content.xml"))
        except KeyError as e:
            raise ExtractError(str(path), "odf: no content.xml") from e
        title, author = _read_meta(zf)

    if kind == "odt":
        yield from _extract_text(content, path, times, parent_id, title, author)
    elif kind == "odp":
        yield from _extract_presentation(content, path, times, parent_id, title)
    elif kind == "ods":
        yield from _extract_spreadsheet(content, path, times, parent_id)


def _read_meta(zf: zipfile.ZipFile) -> tuple[str, str]:
    try:
        meta = parse_xml(zf.read("meta.xml"))
    except (KeyError, etree.XMLSyntaxError):
        return "", ""
    title_el = _find_local(meta, "title")
    creator_el = _find_local(meta, "creator")
    title = "".join(str(t) for t in title_el.itertext()).strip() if title_el is not None else ""
    author = (
        "".join(str(t) for t in creator_el.itertext()).strip() if creator_el is not None else ""
    )
    return title, author


# ── .odt ────────────────────────────────────────────────────────────────────
def _walk_text(el: etree._Element, sec: HeadingSectioner, depth: int = 0) -> None:
    for child in el:
        name = _lname(child.tag)
        if name == "h":
            level = child.get(f"{_TEXT}outline-level")
            sec.add_heading(int(level) if level and level.isdigit() else 1, _text_of(child))
        elif name == "p":
            sec.add_paragraph(_text_of(child), _text_of(child))
        elif name == "list":
            _walk_list(child, sec, depth)
        elif name == "table":
            rows, cells = _odf_table(child)
            sec.add_table(gfm_table(rows), cells)
        elif name in ("section", "text", "frame", "text-box"):
            _walk_text(child, sec, depth)


def _walk_list(el: etree._Element, sec: HeadingSectioner, depth: int) -> None:
    for item in el:
        if _lname(item.tag) != "list-item":
            continue
        for node in item:
            nm = _lname(node.tag)
            if nm in ("p", "h"):
                text = _text_of(node)
                if text:
                    sec.add_list_item(text, text, depth=depth)
            elif nm == "list":
                _walk_list(node, sec, depth + 1)


def _extract_text(
    content: etree._Element, path: Path, times: FileTimes, parent_id: str, title: str, author: str
) -> Iterator[Chunk]:
    body_el = _find_local(content, "text")
    if body_el is None:
        return
    sec = HeadingSectioner(
        parent_id=parent_id, path=path, times=times, kind="odt", title=title, author=author
    )
    _walk_text(body_el, sec)
    yield from sec.finish()


# ── .odp ────────────────────────────────────────────────────────────────────
def _extract_presentation(
    content: etree._Element, path: Path, times: FileTimes, parent_id: str, title: str
) -> Iterator[Chunk]:
    seq = 0
    for page in content.iter(f"{_DRAW}page"):
        name = page.get(f"{_DRAW}name") or f"Slide {seq + 1}"
        paras = [_text_of(p) for p in page.iter() if _lname(p.tag) in ("p", "h")]
        paras = [p for p in paras if p]
        body = "\n".join(paras)
        if not body.strip() and not name.strip():
            continue
        md_lines = [f"# {name}", *paras]
        yield Chunk(
            parent_id=parent_id,
            path=str(path),
            mtime=times.mtime,
            created=times.created,
            inode_changed=times.inode_changed,
            kind="odp",
            body=f"{name}\n{body}".strip(),
            body_struct=[Block(kind="h1", text=name), *[Block(kind="p", text=p) for p in paras]],
            body_md="\n\n".join(md_lines),
            slide=seq + 1,
            title=title,
            heading_path=name,
            chunk_seq=seq,
        )
        seq += 1


# ── .ods ────────────────────────────────────────────────────────────────────
def _odf_table(table_el: etree._Element) -> tuple[list[list[str]], list[str]]:
    """Expand an ODF table honouring column/row repeats; return (grid, cells)."""
    grid: list[list[str]] = []
    cells_flat: list[str] = []
    for row in table_el:
        if _lname(row.tag) != "table-row":
            continue
        row_repeat = _int_attr(row, f"{_TABLE}number-rows-repeated", 1)
        cells: list[str] = []
        for cell in row:
            if _lname(cell.tag) not in ("table-cell", "covered-table-cell"):
                continue
            col_repeat = min(_int_attr(cell, f"{_TABLE}number-columns-repeated", 1), _MAX_COLS)
            value = _text_of(cell)
            cells.extend([value] * col_repeat)
            if len(cells) >= _MAX_COLS:
                break
        while cells and not cells[-1]:  # drop trailing empty (repeat) cells
            cells.pop()
        cells = cells[:_MAX_COLS]
        cells_flat.extend(c for c in cells if c)
        if not cells:  # empty (often repeated 1000s of times) → skip
            continue
        for _ in range(min(row_repeat, _MAX_ROWS)):
            grid.append(cells)
            if len(grid) >= _MAX_ROWS:
                break
        if len(grid) >= _MAX_ROWS:
            break
    return grid, cells_flat


def _int_attr(el: etree._Element, key: str, default: int) -> int:
    val = el.get(key)
    return int(val) if val and val.isdigit() else default


def _extract_spreadsheet(
    content: etree._Element, path: Path, times: FileTimes, parent_id: str
) -> Iterator[Chunk]:
    seq = 0
    for sheet in content.iter(f"{_TABLE}table"):
        name = sheet.get(f"{_TABLE}name") or f"Sheet {seq + 1}"
        grid, cells = _odf_table(sheet)
        if not grid:
            continue
        width = max(len(r) for r in grid)
        grid = [r + [""] * (width - len(r)) for r in grid]
        yield Chunk(
            parent_id=parent_id,
            path=str(path),
            mtime=times.mtime,
            created=times.created,
            inode_changed=times.inode_changed,
            kind="ods",
            body=f"{name}\n" + "\n".join(" ".join(r) for r in grid),
            body_struct=[Block(kind="h1", text=name), *[Block(kind="p", text=c) for c in cells]],
            body_md=f"# {name}\n\n{gfm_table(grid)}",
            title=name,
            heading_path=name,
            chunk_seq=seq,
        )
        seq += 1
