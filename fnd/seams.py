"""Where a chunk boundary cuts through a blockquote, list or fenced block.

The chunk bound splits a file wherever its budget runs out, including inside a
callout or a code listing. Each chunk renders as its own widget, so without a
mark the two halves draw as two blocks: the continuation as a plain quote with a
gap above it. The bound marks both sides of the seam in ``body_md`` with the
containers open across it, outermost first, and the preview closes it up so the
content reads as the one block it is in the file.

A mark is a link reference definition, which markdown renders as nothing:

    [fnd:continued-above]: <> "quote=note list item code"
    [fnd:continues-below]: <> "quote=note list item code"

A level is ``quote`` (``quote=<type>`` for a callout), ``list`` or ``item``;
``code`` means a fence is cut too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Seam", "read_seams", "with_seams"]

_ABOVE = "fnd:continued-above"
_BELOW = "fnd:continues-below"
_MARK = re.compile(rf'^\[({_ABOVE}|{_BELOW})\]: <> "([^"\n]*)"$')


@dataclass(frozen=True, slots=True)
class Seam:
    """The containers open across one chunk boundary, and whether a fence is."""

    levels: tuple[str, ...] = ()
    code: bool = False

    def _mark(self, label: str) -> str:
        return f'[{label}]: <> "{" ".join([*self.levels, *(["code"] if self.code else [])])}"'


def _parse(flags: str) -> Seam:
    words = flags.split()
    return Seam(levels=tuple(w for w in words if w != "code"), code="code" in words)


def with_seams(body_md: str, *, above: Seam | None = None, below: Seam | None = None) -> str:
    """``body_md`` marked as continuing from the chunk above and/or into the one below."""
    if above is not None:
        body_md = f"{above._mark(_ABOVE)}\n{body_md}"
    if below is not None:
        # The blank line keeps the mark out of a paragraph it would otherwise continue.
        body_md = f"{body_md}\n\n{below._mark(_BELOW)}"
    return body_md


def read_seams(body_md: str) -> tuple[Seam | None, Seam | None, str]:
    """The seams ``body_md`` is marked with, and the source without the marks."""
    above = below = None
    first, _, rest = body_md.partition("\n")
    if (m := _MARK.match(first)) and m.group(1) == _ABOVE:
        above, body_md = _parse(m.group(2)), rest
    head, _, last = body_md.rpartition("\n")
    if (m := _MARK.match(last)) and m.group(1) == _BELOW:
        below, body_md = _parse(m.group(2)), head.rstrip("\n")
    return above, below, body_md
