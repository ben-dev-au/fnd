"""Map matched chunks to absolute line positions for the preview scrollbar.

The structural preview stacks one widget per chunk inside a single
``MatchAwareScroll``. A marker is "accurate" when its track cell reflects how
far *down the rendered document* the match sits.

The earlier feed was a bool per chunk, mapped to the bar by chunk *ordinal*
(cell ``i`` lit if ``floor(i * n / size)`` matched). That ignores chunk size:
a match in a short chunk right after a long one was drawn near the top while
the match actually sat ~2/3 down — off by up to half the bar.

Here each chunk is weighted by its rendered line count, taken from the same
source the renderer mounts (``body_md`` else reconstructed block text), so a
match's source-line fraction tracks its rendered-row fraction on the track.
The result feeds the renderer's line-precise path via ``set_match_lines``. The
source-line basis is a proxy for rendered rows (markdown wrapping/blank-line
collapsing shift it slightly), but bounded error beats the ordinal mapping's
half-bar misses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fnd.render import text_has_any_match
from fnd.tui.match_evidence import rendered_text

if TYPE_CHECKING:
    from fnd.matching import MatchSpec
    from fnd.query import FileChunk


def _chunk_source(chunk: FileChunk) -> str:
    """The text whose line count stands in for the chunk's rendered height.

    Routed through :func:`fnd.tui.match_evidence.rendered_text` so the markers
    are measured against exactly the substrate the renderer mounts — the same
    single source of truth the row marker and the landing signal read.
    """
    return rendered_text(chunk)


def structural_match_lines(chunks: list[FileChunk], spec: MatchSpec) -> tuple[list[int], int]:
    """Return ``(sorted absolute match-line positions, total line count)``.

    The total is the summed line count across all chunks (the scroll-track
    length in source-line units). Each matched chunk contributes one position:
    the first rendered line that matches. No-match / empty-query input yields
    ``([], total)`` so the caller clears stale markers while keeping a sane
    track length.

    A chunk whose match exists only in text the renderer does NOT mount
    contributes nothing. It used to contribute a marker at the chunk's top line,
    which put a mark on the track pointing at a place with no highlight — the
    scrollbar promising a match the pane could never show.
    """
    match_lines: list[int] = []
    cursor = 0
    for c in chunks:
        # splitlines() (not split("\n")) so a trailing newline doesn't add a
        # phantom line and an empty source counts as 0 lines, not 1 — both keep
        # the total / fractions closer to the rendered row count.
        lines = _chunk_source(c).splitlines()
        local = next(
            (i for i, ln in enumerate(lines) if text_has_any_match(ln, spec)),
            None,
        )
        if local is not None:
            match_lines.append(cursor + local)
        cursor += len(lines)
    return sorted(set(match_lines)), cursor
