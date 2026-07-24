"""Jupyter notebook (.ipynb) extractor: one chunk per cell.

Markdown cells keep their source verbatim on ``body_md`` (rendered rich);
code cells become a language fence with their text outputs appended. A running
top-level markdown heading becomes each cell's ``heading_path`` so result
labels read sensibly. Parsed with the stdlib ``json`` module — no nbformat.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fnd.extract._fences import fenced
from fnd.extract.base import Block, Chunk, ExtractError
from fnd.fsmeta import read_file_times

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def _source(raw: Any) -> str:
    if isinstance(raw, list):
        return "".join(str(p) for p in raw)
    return raw if isinstance(raw, str) else ""


def _lang(nb: dict[str, Any]) -> str:
    meta = nb.get("metadata", {}) if isinstance(nb.get("metadata"), dict) else {}
    kern = meta.get("kernelspec", {}) if isinstance(meta.get("kernelspec"), dict) else {}
    info = meta.get("language_info", {}) if isinstance(meta.get("language_info"), dict) else {}
    return str(kern.get("language") or info.get("name") or "python")


def _first_heading(src: str) -> str:
    for line in src.splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group(1).strip()
    return ""


def _outputs_text(outputs: Any) -> str:
    if not isinstance(outputs, list):
        return ""
    parts: list[str] = []
    for out in outputs:
        if not isinstance(out, dict):
            continue
        otype = out.get("output_type")
        if otype == "stream":
            parts.append(_source(out.get("text")))
        elif otype in ("execute_result", "display_data"):
            data = out.get("data", {})
            if isinstance(data, dict):
                parts.append(_source(data.get("text/plain")))
        elif otype == "error":
            tb = out.get("traceback")
            if isinstance(tb, list):
                parts.append("\n".join(str(t) for t in tb))
    return "\n".join(p for p in parts if p.strip()).strip()


def extract(path: Path) -> Iterator[Chunk]:
    try:
        yield from _extract_inner(path)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(str(path), f"{type(e).__name__}: {e}") from e


def _extract_inner(path: Path) -> Iterator[Chunk]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        return
    try:
        nb = json.loads(raw)
    except ValueError as e:
        raise ExtractError(str(path), f"not a valid notebook: {e}") from e
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise ExtractError(str(path), "not a valid notebook: missing cells")

    times = read_file_times(path)
    parent_id = _parent_id(path)
    lang = _lang(nb)
    meta = nb.get("metadata", {})
    title = str(meta.get("title", "")) if isinstance(meta, dict) else ""
    section = ""
    seq = 0

    for cell in nb["cells"]:
        if not isinstance(cell, dict):
            continue
        src = _source(cell.get("source"))
        ctype = cell.get("cell_type")
        if ctype == "markdown":
            if not src.strip():
                continue
            heading = _first_heading(src)
            if heading:
                section = heading
            body_md = src
            body = src
            blocks = [Block(kind="p", text=src.strip())]
        elif ctype == "code":
            if not src.strip():
                continue
            outputs = _outputs_text(cell.get("outputs"))
            body_md = fenced(src, lang)
            if outputs:
                body_md += "\n\n" + fenced(outputs)
            body = src if not outputs else f"{src}\n{outputs}"
            blocks = [Block(kind="code", text=src)]
            if outputs:
                blocks.append(Block(kind="code", text=outputs))
        else:  # raw / unknown
            if not src.strip():
                continue
            body_md = fenced(src)
            body = src
            blocks = [Block(kind="code", text=src)]

        yield Chunk(
            parent_id=parent_id,
            path=str(path),
            mtime=times.mtime,
            created=times.created,
            inode_changed=times.inode_changed,
            kind="ipynb",
            body=body,
            body_struct=blocks,
            body_md=body_md,
            heading_path=section,
            title=title,
            chunk_seq=seq,
        )
        seq += 1
