"""EPUB extractor: spine-ordered XHTML → heading sections.

An EPUB is a ZIP whose ``META-INF/container.xml`` points at an OPF package
file; the OPF's ``<spine>`` lists the reading order of the XHTML documents in
``<manifest>``. We read each spine document in order through the shared
``lxml.html`` walker into one ``HeadingSectioner``, so the whole book becomes a
continuous run of heading-section chunks (like a long markdown file), with the
book's title/author from the OPF metadata.

Container/OPF parsing is done with stdlib ``zipfile`` + ``lxml.etree`` (no
ebooklib); element lookups are namespace-agnostic (local-name) to tolerate the
EPUB2/EPUB3 and prefix variations real files ship.
"""

from __future__ import annotations

import hashlib
import posixpath
import zipfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

from fnd.extract._html import parse, walk_html
from fnd.extract._ooxml import reject_if_zip_bomb
from fnd.extract._sectioner import HeadingSectioner
from fnd.extract._xml import parse_xml
from fnd.extract.base import Chunk, ExtractError
from fnd.fsmeta import read_file_times
from fnd.kinds import kind_for_suffix

_XHTML_MEDIA = {"application/xhtml+xml", "text/html"}


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def _lname(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _find_local(root: etree._Element, name: str) -> etree._Element | None:
    for el in root.iter():
        if _lname(el.tag) == name:
            return el
    return None


def _first_text(root: etree._Element, name: str) -> str:
    el = _find_local(root, name)
    if el is not None:
        return "".join(str(t) for t in el.itertext()).strip()
    return ""


def extract(path: Path) -> Iterator[Chunk]:
    try:
        yield from _extract_inner(path)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(str(path), f"{type(e).__name__}: {e}") from e


def _opf_path(zf: zipfile.ZipFile) -> str:
    container = parse_xml(zf.read("META-INF/container.xml"))
    rootfile = _find_local(container, "rootfile")
    full_path = rootfile.get("full-path") if rootfile is not None else None
    if not full_path:
        raise ExtractError(str(zf.filename), "epub: no rootfile in container.xml")
    return full_path


def _extract_inner(path: Path) -> Iterator[Chunk]:
    reject_if_zip_bomb(path)
    times = read_file_times(path)
    kind = kind_for_suffix(path.suffix) or "epub"

    with zipfile.ZipFile(path) as zf:
        opf_path = _opf_path(zf)
        opf = parse_xml(zf.read(opf_path))
        opf_dir = posixpath.dirname(opf_path)

        title = _first_text(opf, "title")
        author = _first_text(opf, "creator")

        manifest_el = _find_local(opf, "manifest")
        spine_el = _find_local(opf, "spine")
        if manifest_el is None or spine_el is None:
            raise ExtractError(str(path), "epub: OPF missing manifest/spine")

        href_by_id: dict[str, tuple[str, str]] = {}
        for item in manifest_el:
            iid = item.get("id")
            if _lname(item.tag) == "item" and iid:
                href_by_id[iid] = (item.get("href", ""), item.get("media-type", ""))

        sec = HeadingSectioner(
            parent_id=_parent_id(path),
            path=path,
            times=times,
            kind=kind,
            title=title,
            author=author,
        )
        for itemref in spine_el:
            if _lname(itemref.tag) != "itemref":
                continue
            href, media = href_by_id.get(itemref.get("idref", ""), ("", ""))
            if not href or media not in _XHTML_MEDIA:
                continue
            entry = posixpath.normpath(posixpath.join(opf_dir, unquote(href)))
            try:
                data = zf.read(entry)
            except KeyError:
                continue
            if data.strip():
                walk_html(parse(data), sec)

        yield from sec.finish()
