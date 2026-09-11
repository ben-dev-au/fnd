"""Line-window chunking, shared by the code / data extractors.

Line-based (not character-based like plain.py) so each chunk's ``line`` is an
exact 1-based source line for deep-links and a fence never splits mid-line.
"""

from __future__ import annotations

from collections.abc import Iterator

# Measured: 97.5% of 160-line Python windows fit in 8k characters, and the
# rest split at a line boundary. A data dump with one record per line reached
# 33,000 WORDS in one window, and every query paid for every one of them.
MAX_WINDOW_CHARS = 8_000


def line_windows(text: str, *, max_lines: int, overlap_lines: int) -> Iterator[tuple[int, str]]:
    """Yield ``(start_line, window_text)`` for overlapping windows of ``text``.

    ``start_line`` is 1-based. The final window is emitted once even if shorter
    than ``max_lines``. A window over ``MAX_WINDOW_CHARS`` is split at line
    boundaries, and a single line over it is split within the line; every
    piece keeps the line it starts on, so deep-links stay exact.
    """
    lines = text.splitlines()
    if not lines:
        return
    step = max(1, max_lines - max(overlap_lines, 0))
    n = len(lines)
    start = 0
    while start < n:
        window = lines[start : start + max_lines]
        yield from _bounded(start + 1, window)
        if start + max_lines >= n:
            break
        start += step


def _bounded(start_line: int, window: list[str]) -> Iterator[tuple[int, str]]:
    joined = "\n".join(window)
    if len(joined) <= MAX_WINDOW_CHARS:
        yield start_line, joined
        return
    held: list[str] = []
    held_line = start_line
    size = 0
    for offset, line in enumerate(window):
        if len(line) > MAX_WINDOW_CHARS:
            if held:
                yield held_line, "\n".join(held)
                held, size = [], 0
            for i in range(0, len(line), MAX_WINDOW_CHARS):
                yield start_line + offset, line[i : i + MAX_WINDOW_CHARS]
            held_line = start_line + offset + 1
            continue
        if held and size + len(line) + 1 > MAX_WINDOW_CHARS:
            yield held_line, "\n".join(held)
            held, size = [], 0
            held_line = start_line + offset
        if not held:
            held_line = start_line + offset
        held.append(line)
        size += len(line) + 1
    if held:
        yield held_line, "\n".join(held)
