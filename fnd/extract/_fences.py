"""Fenced-code-block helper, shared by the code / data / notebook extractors.

The fence length is one backtick longer than the longest backtick run inside
the source, so source that itself contains ``` cannot terminate the fence
early (CommonMark's variable-length-fence rule).
"""

from __future__ import annotations


def fenced(source: str, lang: str = "") -> str:
    """Wrap ``source`` in a ```lang code fence sized to survive embedded backticks."""
    longest = 0
    run = 0
    for ch in source:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{source.rstrip(chr(10))}\n{fence}"
