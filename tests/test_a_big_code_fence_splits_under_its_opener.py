"""A fenced block past the chunk budget splits by lines, each piece under its opener.

A piece without the opener has no source to render, so the preview falls back to
plain lines and the code loses its syntax highlighting.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from fnd.extract import extract
from fnd.extract.base import MAX_CHUNK_CHARS
from fnd.seams import read_seams

_LINES = [f'    private final int field{i} = compute({i}, "padding text");' for i in range(400)]


def _note(prefix: str) -> str:
    fence = [f"{prefix}```java", *(f"{prefix}{line}" for line in _LINES), f"{prefix}```"]
    return "\n".join(
        [
            "# Guide",
            "",
            "## Listing",
            "",
            f"{prefix}[!example]- Complete file: Big.java" if prefix else "Big.java:",
            *([prefix.rstrip()] if prefix else [""]),
            *fence,
            "",
            "The prose after the listing.",
            "",
        ]
    )


def _fenced_lines(body_md: str) -> list[str]:
    tokens = MarkdownIt("gfm-like").parse(read_seams(body_md)[2])
    return [
        line
        for t in tokens
        if t.type == "fence" and t.info == "java"
        for line in t.content.splitlines()
    ]


@pytest.mark.parametrize("prefix", ["", "> "], ids=["plain", "callout"])
def test_every_line_of_a_big_fence_renders_as_highlighted_code(tmp_path: Path, prefix: str) -> None:
    note = tmp_path / "guide.md"
    note.write_text(_note(prefix), encoding="utf-8")
    assert len("\n".join(_LINES)) > MAX_CHUNK_CHARS, "precondition: the fence is over budget"

    chunks = list(extract(note))

    assert all(len(c.body) <= MAX_CHUNK_CHARS for c in chunks)
    rendered = [line for c in chunks for line in _fenced_lines(c.body_md)]
    assert rendered == _LINES, "every line once, in order, inside a java fence"
    assert any("The prose after the listing." in c.body_md for c in chunks)


def test_a_big_fence_deep_in_a_list_never_shows_its_markers_as_code(tmp_path: Path) -> None:
    items = [f"{n}. Step {n}." for n in range(1, 10)]
    fence = ["    ```java", *(f"    {line}" for line in _LINES), "    ```"]
    note = tmp_path / "list.md"
    note.write_text("\n".join(["# T", "", *items, "10. Build it:", "", *fence, "", "after"]))

    chunks = list(extract(note))

    for c in chunks:
        for t in MarkdownIt("gfm-like").parse(c.body_md):
            assert not (t.type == "code_block" and "```" in t.content), "fence marker shown as code"
    words = set(" ".join(c.body for c in chunks).split())
    assert all(f"field{i}" in words for i in range(len(_LINES))), "a code line went missing"
