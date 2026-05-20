"""TXT extractor: fixed-window chunks with overlap.

Per plan §11: TXT has no structural metadata, so we slice on a fixed character
window with overlap so a phrase that straddles a window boundary is still found
intact in the next chunk.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fnd.extract.base import Block, Chunk

WINDOW_CHARS = 1000
OVERLAP_CHARS = 200


def _make_parent_id(path: Path) -> str:
    import hashlib

    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def extract(path: Path) -> Iterator[Chunk]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parent_id = _make_parent_id(path)
    mtime = int(path.stat().st_mtime)

    if not text.strip():
        return

    step = WINDOW_CHARS - OVERLAP_CHARS
    seq = 0
    for start in range(0, len(text), step):
        body = text[start : start + WINDOW_CHARS]
        if not body.strip():
            continue
        # 1-based line of the chunk's first character. Counting newlines
        # in ``text[:start]`` is O(start) per chunk and O(len(text)^2 /
        # step) total — fine for the ≤ few-MB plain-text files this
        # extractor sees in practice; revisit if huge logs land in the
        # corpus.
        start_line = text.count("\n", 0, start) + 1
        yield Chunk(
            parent_id=parent_id,
            path=str(path),
            mtime=mtime,
            kind="txt",
            body=body,
            body_struct=[Block(kind="p", text=body)],
            chunk_seq=seq,
            line=start_line,
        )
        seq += 1
        if start + WINDOW_CHARS >= len(text):
            break
