"""Obsidian callout syntax, shared by the extractor and the preview renderer."""

from __future__ import annotations

import re
from collections.abc import Sequence

from markdown_it.token import Token

__all__ = ["CALLOUT_MARKER", "callout_marker"]

# ``[!type]`` opening a blockquote's first line, with its fold sign and title.
CALLOUT_MARKER = re.compile(r"^\[!([A-Za-z][\w-]*)\]([-+]?)[ \t]*(.*)$")


def callout_marker(tokens: Sequence[Token], i: int) -> re.Match[str] | None:
    """The ``[!type]`` marker when the blockquote opening at ``tokens[i]`` is a callout."""
    follow = tokens[i + 1 : i + 3]
    if tokens[i].type != "blockquote_open" or [t.type for t in follow] != [
        "paragraph_open",
        "inline",
    ]:
        return None
    return CALLOUT_MARKER.match(follow[1].content.partition("\n")[0])
