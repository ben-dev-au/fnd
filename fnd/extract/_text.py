"""Line-window chunking, shared by the code / data extractors.

Line-based (not character-based like plain.py) so each chunk's ``line`` is an
exact 1-based source line for deep-links and a fence never splits mid-line.
"""

from __future__ import annotations

from collections.abc import Iterator


def line_windows(text: str, *, max_lines: int, overlap_lines: int) -> Iterator[tuple[int, str]]:
    """Yield ``(start_line, window_text)`` for overlapping windows of ``text``.

    ``start_line`` is 1-based. The final window is emitted once even if shorter
    than ``max_lines``.
    """
    lines = text.splitlines()
    if not lines:
        return
    step = max(1, max_lines - max(overlap_lines, 0))
    n = len(lines)
    start = 0
    while start < n:
        window = lines[start : start + max_lines]
        yield start + 1, "\n".join(window)
        if start + max_lines >= n:
            break
        start += step
