"""Data / config extractor.

Two shapes, keyed by kind:
* ``csv`` / ``tsv`` — parsed with the stdlib ``csv`` module and rendered as GFM
  pipe tables (previewed as a DataTable), chunked by row groups.
* ``json`` / ``yaml`` / ``toml`` / ``xml`` / ``ini`` — **not interpreted**; the
  raw text is line-windowed and fenced with its language for a syntax-highlighted
  preview. Keeping them uninterpreted is deliberate: it avoids pulling in a
  parser per format and still indexes + previews the content faithfully.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterator
from pathlib import Path

from fnd.extract._fences import fenced
from fnd.extract._tables import gfm_table
from fnd.extract._text import line_windows
from fnd.extract.base import Block, Chunk, ExtractError
from fnd.fsmeta import FileTimes, read_file_times
from fnd.kinds import KIND_BY_ID, kind_for_suffix

WINDOW_LINES = 200
OVERLAP_LINES = 10
ROWS_PER_CHUNK = 50
_DELIMITER = {"csv": ",", "tsv": "\t"}


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
    kind = kind_for_suffix(path.suffix)
    if kind is None:
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return
    times = read_file_times(path)
    parent_id = _parent_id(path)
    if kind in _DELIMITER:
        yield from _tabular(text, path, kind, times, parent_id)
    else:
        yield from _fenced(text, path, kind, times, parent_id)


def _tabular(text: str, path: Path, kind: str, times: FileTimes, parent_id: str) -> Iterator[Chunk]:
    rows = list(csv.reader(text.splitlines(), delimiter=_DELIMITER[kind]))
    if not rows:
        return
    header = rows[0]
    data_rows = rows[1:]
    # Header-only file: still emit one chunk so the columns are findable.
    groups = [
        data_rows[i : i + ROWS_PER_CHUNK] for i in range(0, len(data_rows), ROWS_PER_CHUNK)
    ] or [[]]
    for gi, group in enumerate(groups):
        body_lines = [" ".join(header)]
        blocks = [Block(kind="p", text=" ".join(header))]
        for row in group:
            joined = " ".join(row)
            body_lines.append(joined)
            blocks.append(Block(kind="p", text=joined))
        yield Chunk(
            parent_id=parent_id,
            path=str(path),
            mtime=times.mtime,
            created=times.created,
            inode_changed=times.inode_changed,
            kind=kind,
            body="\n".join(body_lines),
            body_struct=blocks,
            body_md=gfm_table([header, *group]),
            line=gi * ROWS_PER_CHUNK + 2,  # 1-based first data row (header = line 1)
            chunk_seq=gi,
        )


def _fenced(text: str, path: Path, kind: str, times: FileTimes, parent_id: str) -> Iterator[Chunk]:
    lang = KIND_BY_ID[kind].fence_lang
    seq = 0
    for start_line, window in line_windows(
        text, max_lines=WINDOW_LINES, overlap_lines=OVERLAP_LINES
    ):
        if not window.strip():
            continue
        yield Chunk(
            parent_id=parent_id,
            path=str(path),
            mtime=times.mtime,
            created=times.created,
            inode_changed=times.inode_changed,
            kind=kind,
            body=window,
            body_struct=[Block(kind="code", text=window)],
            body_md=fenced(window, lang),
            line=start_line,
            chunk_seq=seq,
        )
        seq += 1
