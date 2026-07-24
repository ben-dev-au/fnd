"""Heading-boundary sectioning, shared by the html / epub / odf extractors.

Generalises the docx/markdown "accumulate blocks, flush one chunk per heading"
pattern into a push-based, format-agnostic helper. A per-format walker feeds it
``add_*`` calls in document order; :meth:`finish` returns the chunks.

Every chunk carries both a markdown serialisation on ``body_md`` (for the
structural preview renderer) and a plain-text ``body_struct`` Block list (for
the snippet pipeline), exactly like the docx extractor.
"""

from __future__ import annotations

from pathlib import Path

from fnd.extract._fences import fenced
from fnd.extract.base import Block, Chunk
from fnd.fsmeta import FileTimes


class HeadingSectioner:
    """Collects rendered blocks and flushes one :class:`Chunk` per heading."""

    def __init__(
        self,
        *,
        parent_id: str,
        path: Path,
        times: FileTimes,
        kind: str,
        title: str = "",
        author: str = "",
    ) -> None:
        self._parent_id = parent_id
        self._path = str(path)
        self._times = times
        self._kind = kind
        self._doc_title = title
        self._author = author
        self._stack: list[str] = []  # ancestor headings, indexed by level-1
        self._blocks: list[Block] = []
        self._body_parts: list[str] = []
        self._md_lines: list[str] = []
        self._seq = 0
        self._has_content = False
        self._chunks: list[Chunk] = []

    def add_heading(self, level: int, text: str) -> None:
        text = text.strip()
        if not text:
            return
        level = min(max(level, 1), 6)
        self._flush()  # a heading opens a new section
        self._stack[level - 1 :] = [text]
        if not self._doc_title and level == 1:
            self._doc_title = text
        self._blocks.append(Block(kind=f"h{level}", text=text))
        self._body_parts.append(text)  # own heading is part of F_BODY
        self._md_lines.append(f"{'#' * level} {text}")
        self._has_content = True

    def add_paragraph(self, md: str, text: str) -> None:
        text = text.strip()
        if not md.strip() and not text:
            return
        if md.strip():
            self._md_lines.append(md)
        if text:
            self._blocks.append(Block(kind="p", text=text))
            self._body_parts.append(text)
        self._has_content = True

    def add_list_item(self, md: str, text: str, *, depth: int = 0, ordered: bool = False) -> None:
        prefix = "1. " if ordered else "- "
        indent = "  " * max(depth, 0)
        self._md_lines.append(f"{indent}{prefix}{md}".rstrip())
        text = text.strip()
        if text:
            self._blocks.append(Block(kind="ol" if ordered else "ul", text=text))
            self._body_parts.append(text)
        self._has_content = True

    def add_code(self, code: str) -> None:
        code = code.strip("\n")
        if not code.strip():
            return
        self._md_lines.append(fenced(code))
        self._blocks.append(Block(kind="code", text=code))
        self._body_parts.append(code)
        self._has_content = True

    def add_quote(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._md_lines.append("\n".join(f"> {ln}" for ln in text.splitlines()))
        self._blocks.append(Block(kind="quote", text=text))
        self._body_parts.append(text)
        self._has_content = True

    def add_table(self, md: str, cell_texts: list[str]) -> None:
        if not md:
            return
        self._md_lines.append(md)
        # Index each cell via body/body_struct so table content is findable and
        # snippets can surface the matched cell.
        for ct in cell_texts:
            ct = ct.strip()
            if ct:
                self._blocks.append(Block(kind="p", text=ct))
                self._body_parts.append(ct)
        self._has_content = True

    def _flush(self) -> None:
        if not self._has_content:
            return
        body = "\n".join(p for p in self._body_parts if p).strip()
        body_md = "\n\n".join(ln for ln in self._md_lines if ln).rstrip()
        if body or self._stack:
            self._chunks.append(
                Chunk(
                    parent_id=self._parent_id,
                    path=self._path,
                    mtime=self._times.mtime,
                    created=self._times.created,
                    inode_changed=self._times.inode_changed,
                    kind=self._kind,
                    body=body,
                    body_struct=self._blocks.copy(),
                    body_md=body_md,
                    heading_path=" > ".join(self._stack),
                    title=self._doc_title,
                    author=self._author,
                    chunk_seq=self._seq,
                )
            )
            self._seq += 1
        self._blocks = []
        self._body_parts = []
        self._md_lines = []
        self._has_content = False

    def finish(self) -> list[Chunk]:
        self._flush()
        return self._chunks
