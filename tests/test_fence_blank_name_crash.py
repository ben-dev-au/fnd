"""Regression: a fenced code block must not crash the preview mount when the
guessed lexer emits a zero-width ``Token.Name`` token.

Some Pygments lexers (e.g. Perl, which ``guess_language`` picks for a bare
fence whose content contains ``□``/``<<``/``|``) emit empty ``Token.Name``
tokens. ``_build_spans`` skips blank tokens when building its neighbour-lookup
map, but the ``Token.Name`` branch then looked the token up unconditionally —
``KeyError`` — which propagated out of ``FNDMarkdownFence.__init__`` and crashed
the whole ``FNDMarkdown`` mount (Textual ``_parse_markdown``).
"""

from __future__ import annotations

import pytest
from pygments.token import Token
from textual.app import App, ComposeResult

from fnd.tui.syntax_theme import _build_spans, highlight_fenced
from fnd.tui.widgets.markdown import FNDMarkdown, FNDMarkdownFence

# The exact chunk body that crashed ``fnd "AVL Tree" -c DSA`` on navigation.
CHECKLIST_FENCE = """### Image Buffer Implementation Checklist

```
□ Core Structure
  □ Allocate 1D array of size width × height
  □ Store width and height
  □ Implement offset calculation: (y × width) + x

□ Colour Handling
  □ Pack ARGB into integers: (a<<24) | (r<<16) | (g<<8) | b
  □ Unpack with shifts and masks: (col >> shift) & 0xFF
```
"""


def test_build_spans_tolerates_blank_name_token() -> None:
    # A zero-width Name token is excluded from the neighbour map but was still
    # looked up in the Name branch -> KeyError. It must be skipped instead.
    tokens = [(Token.Text, "□ "), (Token.Name, ""), (Token.Name, "Core")]
    spans = _build_spans(tokens)  # must not raise
    assert isinstance(spans, list)


def test_highlight_fenced_handles_checklist_content() -> None:
    code = CHECKLIST_FENCE.split("```")[1].strip("\n")
    content = highlight_fenced(code, None)  # must not raise
    assert "Core Structure" in content.plain


class _Host(App[None]):
    def __init__(self, md: FNDMarkdown) -> None:
        self.md = md
        super().__init__()

    def compose(self) -> ComposeResult:
        yield self.md


@pytest.mark.asyncio
async def test_checklist_chunk_mounts_without_crashing() -> None:
    md = FNDMarkdown(CHECKLIST_FENCE)
    async with _Host(md).run_test():
        await md.build_done.wait()
        assert len(list(md.query(FNDMarkdownFence))) == 1
