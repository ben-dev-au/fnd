"""HTML / XHTML extractor: one chunk per heading section.

Parses the document with the shared ``lxml.html`` walker and feeds a
``HeadingSectioner``, so a standalone ``.html`` page renders through the same
structural path as markdown/docx (headings, lists, tables, code, prose) with
matching highlighting and scroll-to-match.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from fnd.extract._html import parse, walk_html
from fnd.extract._sectioner import HeadingSectioner
from fnd.extract.base import Chunk, ExtractError
from fnd.fsmeta import read_file_times
from fnd.kinds import kind_for_suffix


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def extract(path: Path) -> Iterator[Chunk]:
    try:
        yield from _extract_inner(path)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(str(path), f"{type(e).__name__}: {e}") from e


def _extract_inner(path: Path) -> Iterator[Chunk]:
    data = path.read_bytes()
    if not data.strip():
        return
    times = read_file_times(path)
    kind = kind_for_suffix(path.suffix) or "html"
    root = parse(data)

    title = ""
    title_el = root.find(".//title")
    if title_el is not None and title_el.text:
        title = title_el.text.strip()

    sec = HeadingSectioner(
        parent_id=_parent_id(path), path=path, times=times, kind=kind, title=title
    )
    walk_html(root, sec)
    yield from sec.finish()
