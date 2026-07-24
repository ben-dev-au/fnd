"""Source-code extractor: line-window chunks rendered as language fences.

Covers every code kind in the registry (python, cpp, go, …). Each window is
indexed as raw source (``body``) and previewed as a syntax-highlighted
```lang fence (``body_md``) via the existing FNDMarkdown fence renderer, so
code files highlight and scroll-to-match like every other structural preview.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from fnd.extract._fences import fenced
from fnd.extract._text import line_windows
from fnd.extract.base import Block, Chunk, ExtractError
from fnd.fsmeta import read_file_times
from fnd.kinds import KIND_BY_ID, kind_for_suffix

# Line-based windows keep each chunk's ``line`` exact for editor deep-links and
# never split a fence mid-line. Overlap catches matches straddling a boundary.
WINDOW_LINES = 160
OVERLAP_LINES = 12


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
    if kind is None:  # dispatch only routes registered suffixes here
        return
    lang = KIND_BY_ID[kind].fence_lang
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return
    times = read_file_times(path)
    parent_id = _parent_id(path)

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
