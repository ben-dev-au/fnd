"""Fenced-code-block helper, shared by the code / data / notebook extractors.

The fence length is one backtick longer than the longest backtick run inside
the source, so source that itself contains ``` cannot terminate the fence
early (CommonMark's variable-length-fence rule).
"""

from __future__ import annotations

import re

_BACKTICK_RUN = re.compile("`+")


def fenced(source: str, lang: str = "") -> str:
    """Wrap ``source`` in a ```lang code fence sized to survive embedded backticks."""
    longest = max((len(run) for run in _BACKTICK_RUN.findall(source)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{source.rstrip(chr(10))}\n{fence}"


def unfenced(markdown: str) -> tuple[str, str] | None:
    """``(lang, code)`` when ``markdown`` is one fence as :func:`fenced` writes it."""
    head, _, rest = markdown.partition("\n")
    fence = head[: len(head) - len(head.lstrip("`"))]
    if len(fence) < 3 or not rest.endswith("\n" + fence):
        return None
    return head[len(fence) :], rest[: -len(fence) - 1]
