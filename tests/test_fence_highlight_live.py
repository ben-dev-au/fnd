"""In-code-fence search-term highlighting on the live FNDMarkdown path.

The live preview mounts ``FNDMarkdown`` (not the off-by-default hybrid
renderer). Stock ``MarkdownFence`` renders syntax colours only, so query
terms inside a ``` fence ``` went unhighlighted. ``FNDMarkdownFence``
overlays the match spans on top of the lexer colours.

One document with two fences sharing the document's single match spec —
the same shape as the real app, where every chunk of a result gets the
one ``_current_match_spec``. The matching fence is highlighted; the
non-matching fence is left clean (no over-highlight bleed).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from fnd.matching import MatchSpec
from fnd.render import HIGHLIGHT_STYLE
from fnd.tui.widgets.markdown import FNDMarkdown, FNDMarkdownFence


class _Host(App[None]):
    def __init__(self, md: FNDMarkdown) -> None:
        self.md = md
        super().__init__()

    def compose(self) -> ComposeResult:
        yield self.md


def _highlighted_words(content: object) -> list[str]:
    plain = content.plain  # type: ignore[attr-defined]
    return [
        plain[s.start : s.end]
        for s in (getattr(content, "spans", ()) or ())
        if str(s.style) == HIGHLIGHT_STYLE
    ]


@pytest.mark.asyncio
async def test_fence_highlights_only_the_matching_fence() -> None:
    source = (
        "```python\ndef hit():\n    return needle\n```\n\n"
        "```python\ndef miss():\n    return other\n```"
    )
    md = FNDMarkdown(source, match_spec=MatchSpec.from_query("needle"))
    async with _Host(md).run_test():
        await md.build_done.wait()
        fences = list(md.query(FNDMarkdownFence))
        assert len(fences) == 2, f"expected two code fences, got {len(fences)}"

        first = fences[0]._highlighted_code
        second = fences[1]._highlighted_code

        # Matching fence: 'needle' highlighted, lexer syntax spans retained.
        assert _highlighted_words(first) == ["needle"]
        assert any(str(s.style) != HIGHLIGHT_STYLE for s in (first.spans or ())), (
            "expected lexer syntax spans to survive under the overlay"
        )

        # Non-matching fence: no highlight bleed.
        assert _highlighted_words(second) == []


@pytest.mark.asyncio
async def test_fence_highlights_subword_of_underscore_identifier() -> None:
    """Regression: ``iterator`` inside ``recursive_directory_iterator`` must
    highlight, like a standalone ``iterator`` does.

    The ``en_stem`` analyzer splits on underscore, so ``iterator`` is an indexed
    token of ``recursive_directory_iterator`` and a search for it finds the
    chunk. The highlighter previously tokenised doc text with ``\\w+`` (keeps
    underscore), saw one token that failed to stem-match, and left that one
    occurrence unhighlighted while sibling plain matches lit up — the exact
    "one code-block match isn't highlighted" field report. Both occurrences must
    now highlight identically."""
    source = (
        "```cpp\n"
        "for (const auto& e : fs::recursive_directory_iterator(root)) {\n"
        "    use(e);  // plain iterator below\n"
        "}\n"
        "```"
    )
    md = FNDMarkdown(source, match_spec=MatchSpec.from_query("iterator"))
    async with _Host(md).run_test():
        await md.build_done.wait()
        fence = next(iter(md.query(FNDMarkdownFence)))
        content = fence._highlighted_code
        plain = content.plain

        # The sub-token inside the underscore identifier is highlighted.
        sub = plain.index("iterator")  # first occurrence: inside the identifier
        assert sub > plain.index("recursive_directory_"), "sanity: identifier case"
        assert any(
            s.start == sub and s.end == sub + len("iterator") and str(s.style) == HIGHLIGHT_STYLE
            for s in (content.spans or ())
        ), "iterator sub-token of recursive_directory_iterator not highlighted"

        # And every 'iterator' occurrence highlights (the identifier + the comment).
        assert plain.count("iterator") == _highlighted_words(content).count("iterator")
