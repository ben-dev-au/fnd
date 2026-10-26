"""Markdown extractor: one chunk per heading section.

Walks the markdown-it-py token stream, splits on headings, tracks an ancestor
stack so each section ships its full ``heading_path`` like ``Notes > Methods >
Sampling``.

Each chunk also carries the verbatim markdown source for its section in
``body_md``: the TUI's structural preview renderer (Textual Markdown
widget) consumes this. ``body_struct`` continues to carry a flat list of
plain-text Blocks for the snippet pipeline; the two are intentionally
separate so the snippet path doesn't end up showing markdown markers like
``**bold**`` or ``# heading`` to the user.
"""

from __future__ import annotations

import dataclasses
import re
from bisect import bisect_right
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from fnd.callout_syntax import callout_marker
from fnd.extract.base import (
    MAX_CHUNK_CHARS,
    MAX_TABLE_CHARS,
    Block,
    Chunk,
    Container,
    ExtractError,
    file_parent_id,
)
from fnd.fsmeta import FileTimes, read_file_times
from fnd.text_canon import decode

# CommonMark plus the GFM table rule: without it a table parses as a paragraph
# of raw pipes, which inflates the body over the chunk budget and renders as raw
# source in the preview. Only the table rule is added, so nothing else about the
# parse changes.
_md = MarkdownIt("commonmark").enable("table")

# Token types that carry "real" content. A section is worth flushing as a
# chunk when *any* of these appeared inside it, even if no `inline`
# tokens did. Pre-fix the extractor silently dropped sections that
# contained only a fenced code block (``code`` kind, no inline children),
# losing them from the index entirely.
_CONTENT_TOKEN_TYPES: frozenset[str] = frozenset(
    {
        "inline",
        "fence",
        "code_block",
        "table_open",
        "bullet_list_open",
        "ordered_list_open",
        "blockquote_open",
        "hr",
    }
)


_CONTAINERS = {
    "blockquote_open": "quote",
    "bullet_list_open": "list",
    "ordered_list_open": "list",
    "list_item_open": "item",
}
_LEAVES = frozenset({"paragraph_open", "heading_open", "fence", "code_block", "table_open"})


class _Containers:
    """The containers open around each leaf block of a parse, looked up by source line."""

    def __init__(self, tokens: list[Any], source_lines: list[str]) -> None:
        last_numbers = _ordered_list_last_numbers(tokens)
        stack: list[Container] = []
        self._starts: list[int] = []
        self._ends: list[int] = []
        self._chains: list[tuple[Container, ...]] = []
        for i, tok in enumerate(tokens):
            if tok.type in _CONTAINERS:
                start, end = tok.map or (-1, -1)
                detail = opener = ""
                if marker := callout_marker(tokens, i):
                    detail = marker.group(1)
                elif i in last_numbers:
                    detail = str(last_numbers[i])
                elif tok.type == "list_item_open" and 0 <= start < len(source_lines):
                    # `- - a` opens two items on one line: the inner one is the second marker.
                    depth = sum(c.kind == "item" and c.start == start for c in stack)
                    opener = _item_opener(source_lines[start], depth)
                stack.append(Container(_CONTAINERS[tok.type], start, end, detail, opener))
            elif tok.nesting == -1 and tok.type.replace("_close", "_open") in _CONTAINERS:
                stack.pop()
            elif tok.type in _LEAVES and tok.map and stack:
                self._starts.append(tok.map[0])
                self._ends.append(tok.map[1])
                self._chains.append(tuple(stack))

    def at(self, line: int) -> tuple[Container, ...]:
        """The containers holding the leaf block at absolute source ``line``."""
        k = bisect_right(self._starts, line) - 1
        return self._chains[k] if k >= 0 and line < self._ends[k] else ()

    def tag(self, blocks: list[Block], offset: int) -> list[Block]:
        """``blocks``, spans relative to ``offset``, each with the containers holding it."""
        out: list[Block] = []
        for block in blocks:
            chain = self.at(block.span[0] + offset) if block.span is not None else ()
            out.append(dataclasses.replace(block, containers=chain) if chain else block)
        return out


def _ordered_list_last_numbers(tokens: list[Any]) -> dict[int, int]:
    """Each ordered list's last item number, keyed by its opening token's index."""
    counts: dict[int, int] = {}
    open_lists: list[int] = []
    for i, tok in enumerate(tokens):
        if tok.type == "ordered_list_open":
            open_lists.append(i)
            counts[i] = 0
        elif tok.type == "ordered_list_close":
            open_lists.pop()
        elif tok.type == "list_item_open" and open_lists:
            if tokens[open_lists[-1]].level == tok.level - 1:
                counts[open_lists[-1]] += 1
    starts = {i: tokens[i].attrGet("start") for i in counts}
    return {i: (1 if starts[i] is None else int(starts[i])) + n - 1 for i, n in counts.items()}


# One step of a line's container prefix: whitespace, a quote marker or a list marker.
_PREFIX_PART = re.compile(r"[ \t]+|>|[-+*]|\d{1,9}[.)]")


def _item_opener(line: str, depth: int) -> str:
    """``line`` up to its ``depth``-th list marker, from 0, earlier markers blanked."""
    # Whitespace stays verbatim: a tab blanked to one space moves every column after it.
    out: list[str] = []
    markers = 0
    pos = 0
    while (part := _PREFIX_PART.match(line, pos)) is not None:
        text = part.group(0)
        if text[0] in " \t>":
            out.append(text)
        elif markers == depth:
            return "".join(out) + text
        else:
            out.append(" " * len(text))
            markers += 1
        pos = part.end()
    return ""


def _flush_section(
    *,
    path: Path,
    parent_id: str,
    times: FileTimes,
    heading_stack: list[str],
    blocks: list[Block],
    body_text_parts: list[str],
    body_md: str,
    has_content: bool,
    seq: int,
    section_start_line: int,
    containers: _Containers,
) -> Chunk | None:
    if not has_content:
        return None
    body = "\n".join(p for p in body_text_parts if p).strip()
    heading_path = " > ".join(heading_stack)
    # F_BODY = chunk's own visible text (own heading + paragraphs).
    # Ancestor heading_path is searched via F_HEADING_PATH to order sections,
    # NOT baked into body: inheriting parent-heading text into every
    # descendant's body inflates scores and surfaces chunks whose
    # visible content has no match.
    return Chunk(
        parent_id=parent_id,
        path=str(path),
        mtime=times.mtime,
        created=times.created,
        inode_changed=times.inode_changed,
        kind="md",
        body=body,
        body_struct=containers.tag(blocks, section_start_line),
        body_md=body_md,
        heading_path=heading_path,
        title=heading_stack[0] if heading_stack else "",
        chunk_seq=seq,
        # `section_start_line` is 0-based (token.map index); deep-link
        # templates want 1-based.
        line=section_start_line + 1,
    )


def _span(tokens: list[Any], i: int, section_start_line: int) -> tuple[int, int] | None:
    """Line span of the block token at ``i``, relative to its section source.

    An ``inline`` token carries no map; its opening block token, the one just
    before it, does. Fence and code tokens carry their own, fences included.
    """
    tok = tokens[i]
    if tok.type == "inline" and i > 0:
        tok = tokens[i - 1]
    if not tok.map:
        return None
    return (tok.map[0] - section_start_line, tok.map[1] - section_start_line)


def _table_blocks(tokens: list[Any], i: int, section_start_line: int) -> tuple[list[Block], int]:
    """The table opening at ``tokens[i]`` as blocks, and the index just past it.

    One block while the table fits ``MAX_TABLE_CHARS`` (see fnd.extract._bound for
    why a table stays whole). Past that, runs of body rows under the chunk budget,
    each carrying the header's text and span so every piece renders as a table.
    """
    span = _span(tokens, i, section_start_line)
    rows: list[tuple[tuple[int, int] | None, list[str]]] = []
    body_start: int | None = None
    j = i + 1
    while j < len(tokens) and tokens[j].type != "table_close":
        tok = tokens[j]
        if tok.type == "tbody_open" and tok.map:
            body_start = tok.map[0] - section_start_line
        elif tok.type == "tr_open":
            rows.append((_span(tokens, j, section_start_line), []))
        elif tok.type == "inline" and rows and (cell := tok.content.strip()):
            rows[-1][1].append(cell)
        j += 1
    text = " ".join(cell for _, cells in rows for cell in cells)
    if not text:
        return [], j + 1
    body = [
        (s, " ".join(cells))
        for s, cells in rows
        if s and body_start is not None and s[0] >= body_start
    ]
    if len(text) <= MAX_TABLE_CHARS or span is None or body_start is None or not body:
        return [Block(kind="table", text=text, span=span)], j + 1
    header = " ".join(cell for s, cells in rows if s and s[0] < body_start for cell in cells)
    head = (span[0], body_start)
    pieces: list[Block] = []
    run: list[tuple[tuple[int, int], str]] = []
    size = len(header)
    for row_span, row_text in body:
        if run and size + len(row_text) + 1 > MAX_CHUNK_CHARS:
            pieces.append(_table_piece(header, run, head))
            run, size = [], len(header)
        run.append((row_span, row_text))
        size += len(row_text) + 1
    pieces.append(_table_piece(header, run, head))
    return pieces, j + 1


def _table_piece(
    header: str, run: list[tuple[tuple[int, int], str]], head: tuple[int, int]
) -> Block:
    text = " ".join([header, *(row_text for _, row_text in run)]).strip()
    return Block(kind="table", text=text, span=(run[0][0][0], run[-1][0][1]), head=head)


def _code_blocks(
    tokens: list[Any],
    i: int,
    section_start_line: int,
    source_lines: list[str],
    containers: _Containers,
) -> list[Block]:
    """The code at ``tokens[i]``: past the budget, runs of lines under a fence's opener."""
    tok = tokens[i]
    code = tok.content.strip()
    if not code:
        return []
    span = _span(tokens, i, section_start_line)
    fence = tok.type == "fence"
    if len(code) <= MAX_CHUNK_CHARS or span is None:
        return [Block(kind="code", text=code, span=span)]
    if fence and not _reparses([source_lines[tok.map[0]]], containers.at(tok.map[0]), "fence"):
        return [Block(kind="code", text=code, span=span)]
    first = span[0] + fence
    head = (span[0], first) if fence else None
    lines = tok.content.splitlines()
    return [
        Block(
            kind="code",
            text="\n".join(lines[start:end]).strip(),
            # The last piece runs to the token's end, closing fence included.
            span=(first + start, first + end if end < len(lines) else span[1]),
            head=head,
        )
        for start, end in _line_runs(lines, lambda _: True)
    ]


def _paragraph_blocks(
    tokens: list[Any],
    i: int,
    section_start_line: int,
    source_lines: list[str],
    containers: _Containers,
) -> list[Block]:
    """The paragraph whose inline is ``tokens[i]``: past the budget, runs of its lines."""
    text = tokens[i].content.strip()
    if not text:
        return []
    span = _span(tokens, i, section_start_line)
    if len(text) <= MAX_CHUNK_CHARS or span is None:
        return [Block(kind="p", text=text, span=span)]
    top = span[0] + section_start_line
    chain = containers.at(top)

    def can_cut(k: int) -> bool:
        # A lazy line, or one reading as a list or heading alone, would leave the paragraph.
        # The line alone first: the window costs 30 times as much, and adds only refusals.
        line = source_lines[top + k]
        return _reparses([line], chain, "paragraph_open") and _reparses(
            _construct_window(source_lines, top + k), chain, "paragraph_open"
        )

    lines = tokens[i].content.splitlines()
    return [
        Block(
            kind="p",
            text="\n".join(lines[start:end]).strip(),
            span=(span[0] + start, span[0] + end),
        )
        for start, end in _line_runs(lines, can_cut)
    ]


# A link reference label holds at most 999 characters; its destination and title
# take up to two lines after it.
_LABEL_LIMIT = 999


def _construct_window(source_lines: list[str], start: int) -> list[str]:
    """``source_lines`` from ``start``, enough to hold any reference definition there."""
    end, size = start, 0
    while end < len(source_lines) and size <= _LABEL_LIMIT:
        size += len(source_lines[end]) + 1
        end += 1
    return source_lines[start : end + 2]


def _line_runs(lines: list[str], can_cut: Callable[[int], bool]) -> list[tuple[int, int]]:
    """``(start, end)`` runs of ``lines`` under the budget, cut only where ``can_cut``."""
    cuts = [0]
    size = 0
    for k, line in enumerate(lines):
        if k > cuts[-1] and size + len(line) + 1 > MAX_CHUNK_CHARS and can_cut(k):
            cuts.append(k)
            size = 0
        size += len(line) + 1
    return list(zip(cuts, [*cuts[1:], len(lines)], strict=True))


def _reparses(lines: list[str], chain: tuple[Container, ...], leaf: str) -> bool:
    """Whether a piece starting on ``lines[0]`` reopens there as a ``leaf`` inside ``chain``."""
    # The item markers are all a continuation replays (see fnd.extract._bound); the
    # lines after the first catch a construct spanning the cut, such as a reference.
    openers = [c.opener for c in chain if c.kind == "item"]
    open_kinds: list[str] = []
    for tok in _md.parse("\n".join([*openers, *lines])):
        if tok.type in _CONTAINERS:
            open_kinds.append(_CONTAINERS[tok.type])
        elif tok.nesting == -1 and tok.type.replace("_close", "_open") in _CONTAINERS:
            open_kinds.pop()
        elif tok.type in _LEAVES:
            starts_here = tok.map is not None and tok.map[0] == len(openers)
            return tok.type == leaf and starts_here and open_kinds == [c.kind for c in chain]
    return False


def _section_source(source_lines: list[str], start_line: int, end_line: int) -> str:
    """Slice the raw source for a heading section.

    ``start_line`` is the line of the section's heading_open (inclusive),
    ``end_line`` is the line of the *next* heading_open (exclusive), or
    ``len(source_lines)`` for the final section. The slice is verbatim
    so tables, fenced code, lists, blockquotes, everything markdown-it
    parses, round-trips into the renderer untouched.
    """
    start = max(start_line, 0)
    end = max(min(end_line, len(source_lines)), start)
    return "\n".join(source_lines[start:end]).rstrip()


def extract(path: Path) -> Iterator[Chunk]:
    try:
        yield from _extract_inner(path)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(str(path), f"{type(e).__name__}: {e}") from e


def _blank_frontmatter(source: str) -> str:
    """Replace a leading ``---`` frontmatter block with blank lines.

    Frontmatter is metadata: it's parsed separately into meta_blob and tags.
    Left in place, CommonMark reads it as thematic-break + paragraph + setext
    heading, so ``tags: [recipe]`` lands in the searchable body and the fence
    can be mistaken for a heading.

    Blanked rather than removed so every downstream line number still matches
    the file on disk: F_LINE feeds deep links like ``code -g {path}:{line}``.
    """
    if not source.startswith("---"):
        return source
    lines = source.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return source
    # Preserve the file's own line ending so blanking doesn't introduce mixed
    # endings when the body below keeps its original CRLF.
    ending = "\r\n" if lines[0].endswith("\r\n") else "\n"
    for i in range(1, len(lines)):
        if lines[i].rstrip() in ("---", "..."):
            return ending * (i + 1) + "".join(lines[i + 1 :])
    return source  # unterminated fence: treat as content, don't eat the file


def _extract_inner(path: Path) -> Iterator[Chunk]:
    source = decode(path.read_bytes())
    if not source.strip():
        return

    # Before both the parse and the line split, so neither the body nor the
    # preview's body_md picks frontmatter up.
    source = _blank_frontmatter(source)

    parent_id = file_parent_id(path)
    times = read_file_times(path)
    tokens = _md.parse(source)
    source_lines = source.splitlines()
    containers = _Containers(tokens, source_lines)
    total_lines = len(source_lines)

    heading_stack: list[str] = []  # current ancestor headings, indexed by level (1..6)
    blocks: list[Block] = []
    body_parts: list[str] = []
    seq = 0
    in_heading = False
    pending_heading_level = 0
    section_start_line = 0  # source line where the current section begins
    section_has_content = False  # any non-trivial token seen since last flush

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "heading_open":
            # Source line of the new heading: defines the boundary
            # for the *previous* section's slice.
            heading_line = tok.map[0] if tok.map else section_start_line
            # Flush the section we were accumulating, slicing source up
            # to (but not including) this heading.
            chunk = _flush_section(
                path=path,
                parent_id=parent_id,
                times=times,
                heading_stack=heading_stack,
                blocks=blocks,
                body_text_parts=body_parts,
                body_md=_section_source(source_lines, section_start_line, heading_line),
                has_content=section_has_content,
                seq=seq,
                section_start_line=section_start_line,
                containers=containers,
            )
            if chunk is not None:
                yield chunk
                seq += 1
            blocks = []
            body_parts = []
            section_start_line = heading_line
            section_has_content = True  # the new heading itself counts as content

            in_heading = True
            pending_heading_level = int(tok.tag[1])  # h1 -> 1
            i += 1
            continue

        if tok.type == "heading_close":
            in_heading = False
            i += 1
            continue

        if tok.type == "table_open":
            table_blocks, i = _table_blocks(tokens, i, section_start_line)
            blocks.extend(table_blocks)
            body_parts.extend(b.text for b in table_blocks)
            section_has_content = True
            continue

        if in_heading and tok.type == "inline":
            text = tok.content.strip()
            # Truncate the heading stack to the level above and push.
            heading_stack[pending_heading_level - 1 :] = [text]
            blocks.append(
                Block(
                    kind=f"h{pending_heading_level}",
                    text=text,
                    span=_span(tokens, i, section_start_line),
                )
            )
            # Index the chunk's own heading as part of its searchable
            # body so a query that matches the heading text returns this
            # chunk. Ancestor headings live in heading_path (separately
            # boosted); only this chunk's own heading goes into F_BODY.
            body_parts.append(text)
            i += 1
            continue

        if tok.type == "inline":
            paragraph = _paragraph_blocks(tokens, i, section_start_line, source_lines, containers)
            if paragraph:
                blocks.extend(paragraph)
                body_parts.extend(b.text for b in paragraph)
                section_has_content = True
            i += 1
            continue

        # Fenced / indented code carries its text on ``tok.content`` with
        # no ``inline`` children, so unlike paragraphs it would never reach
        # F_BODY via the inline branch above. Index it so code-only matches
        # are findable, and emit a Block so snippets can surface the line.
        if tok.type in ("fence", "code_block"):
            code_blocks = _code_blocks(tokens, i, section_start_line, source_lines, containers)
            blocks.extend(code_blocks)
            body_parts.extend(b.text for b in code_blocks)
            section_has_content = True
            i += 1
            continue

        # Other token types (lists, hr, tables, blockquotes): we don't
        # expand them into the legacy Block list (the inline children that
        # DO appear already cover the searchable text), but flag the section
        # as having content so a table-only section flushes as its own chunk.
        if tok.type in _CONTENT_TOKEN_TYPES:
            section_has_content = True
        i += 1

    # Final flush: slice to end of file.
    chunk = _flush_section(
        path=path,
        parent_id=parent_id,
        times=times,
        heading_stack=heading_stack,
        blocks=blocks,
        body_text_parts=body_parts,
        body_md=_section_source(source_lines, section_start_line, total_lines),
        has_content=section_has_content,
        seq=seq,
        section_start_line=section_start_line,
        containers=containers,
    )
    if chunk is not None:
        yield chunk
