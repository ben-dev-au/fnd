"""Fenced code blocks render with the FND truecolor syntax palette.

The live preview mounts ``FNDMarkdown``; its ``FNDMarkdownFence`` overrides
``highlight`` to map Pygments tokens through ``FNDSyntaxTheme`` (granular
fixed-hex colours) and to retag function call sites. These tests assert the
per-role colours land on the right spans, on the same live mount path the app
uses, and that an unknown fence language degrades gracefully.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from fnd.tui.syntax_theme import inline_code_spans
from fnd.tui.widgets.markdown import FNDMarkdown, FNDMarkdownFence, FNDMarkdownParagraph


class _Host(App[None]):
    def __init__(self, md: FNDMarkdown) -> None:
        self.md = md
        super().__init__()

    def compose(self) -> ComposeResult:
        yield self.md


def _style_at(content: object, sub: str) -> set[str]:
    """Styles covering the first occurrence of ``sub`` in ``content``."""
    plain = content.plain  # type: ignore[attr-defined]
    i = plain.index(sub)
    return {str(s.style) for s in (getattr(content, "spans", ()) or ()) if s.start <= i < s.end}


@pytest.mark.asyncio
async def test_fence_roles_carry_palette_colours() -> None:
    source = (
        "```python\n"
        "@deco\n"
        "def hit(x):\n"
        "    n = 42  # note\n"
        '    return helper(x) + "s" + print(n)\n'
        "```"
    )
    md = FNDMarkdown(source)
    async with _Host(md).run_test():
        await md.build_done.wait()
        fences = list(md.query(FNDMarkdownFence))
        assert len(fences) == 1
        content = fences[0]._highlighted_code

        # keyword, definition, decorator, number, comment, string
        assert "#CC76D1" in _style_at(content, "def")
        assert "#FD8A38" in _style_at(content, "hit")
        assert "#7BB7E2" in _style_at(content, "deco")
        assert "#E0AF68" in _style_at(content, "42")
        assert "#4E6B6E" in _style_at(content, "# note")
        assert "#CACACA" in _style_at(content, '"s"')

        # call-site heuristic: a bare-name call goes function-orange, but a
        # builtin call keeps its distinct builtin colour (not retagged).
        assert "#FD8A38" in _style_at(content, "helper")
        assert "#79E6F3" in _style_at(content, "print")


@pytest.mark.asyncio
async def test_cpp_name_roles_are_differentiated() -> None:
    # Pygments tags namespace/type/variable all as bare Name; the positional
    # heuristics must split them so a C++ line isn't a sea of blue.
    md = FNDMarkdown("```cpp\nstd::vector<const SaleLineItem*> _lineItems;\n```")
    async with _Host(md).run_test():
        await md.build_done.wait()
        content = next(iter(md.query(FNDMarkdownFence)))._highlighted_code
        assert "#79E6F3" in _style_at(content, "std")  # namespace (before ::)
        assert "#5ADECD" in _style_at(content, "SaleLineItem")  # PascalCase type
        assert "#5ADECD" in _style_at(content, "vector")  # qualified (after ::)
        assert "#7BB7E2" in _style_at(content, "_lineItems")  # variable


@pytest.mark.asyncio
async def test_brackets_are_rainbow_coloured_by_depth() -> None:
    md = FNDMarkdown("```python\nf(g(x))\n```")
    async with _Host(md).run_test():
        await md.build_done.wait()
        content = next(iter(md.query(FNDMarkdownFence)))._highlighted_code
        plain = content.plain

        def style_at_index(i: int) -> set[str]:
            return {str(s.style) for s in content.spans if s.start <= i < s.end}

        opens = [i for i, ch in enumerate(plain) if ch == "("]
        closes = [i for i, ch in enumerate(plain) if ch == ")"]
        # Depth-0 and depth-1 openers differ; each closer matches its opener.
        assert style_at_index(opens[0]) != style_at_index(opens[1])
        assert style_at_index(opens[0]) & style_at_index(closes[1])  # outer pair
        assert style_at_index(opens[1]) & style_at_index(closes[0])  # inner pair


def test_inline_code_spans_are_calm() -> None:
    # Names/calls/types stay coloured; operator/separator chars go neutral so
    # paths and flags don't get red slashes/dashes mid-prose.
    def style_of(code: str, sub: str) -> str | None:
        i = code.index(sub)
        hits = [str(s.style) for s in inline_code_spans(code) if s.start <= i < s.end]
        return hits[0] if hits else None

    assert style_of("Searcher.search()", "search") == "#FD8A38"  # call
    assert style_of("Searcher.search()", ".") == "#A9B1D6"  # separator neutral
    assert style_of("std::vector", "vector") == "#5ADECD"  # type still resolves
    assert style_of("std::vector", ":") == "#A9B1D6"  # :: rendered neutral
    assert style_of("fnd/tui/app.py", "/") == "#A9B1D6"  # path slash neutral
    assert style_of("--rebuild", "-") == "#A9B1D6"  # flag dash neutral


@pytest.mark.asyncio
async def test_inline_code_highlighted_in_prose() -> None:
    md = FNDMarkdown("Call `Searcher.search()` to begin.")
    async with _Host(md).run_test():
        await md.build_done.wait()
        para = next(iter(md.query(FNDMarkdownParagraph)))
        content = para._content
        # The inline call name is orange; surrounding prose is not.
        assert "#FD8A38" in _style_at(content, "search")
        assert "#FD8A38" not in _style_at(content, "begin")


@pytest.mark.asyncio
async def test_rust_scope_uses_single_colon_token() -> None:
    # Rust lexes `::` as a single Punctuation token (unlike C++'s two `:`),
    # so the scope/type heuristics must handle both forms.
    md = FNDMarkdown("```rust\nlet v = std::vec::Vec::new();\n```")
    async with _Host(md).run_test():
        await md.build_done.wait()
        content = next(iter(md.query(FNDMarkdownFence)))._highlighted_code
        assert "#79E6F3" in _style_at(content, "std")  # namespace (before ::)
        assert "#FD8A38" in _style_at(content, "new")  # call


@pytest.mark.asyncio
async def test_unknown_language_degrades_gracefully() -> None:
    # An explicitly invalid language tag exercises the ClassNotFound -> "text"
    # fallback (a bare fence resolves via guess_language and never hits it).
    md = FNDMarkdown("```definitely_not_a_real_lexer\njust plain text 123\n```")
    async with _Host(md).run_test():
        await md.build_done.wait()
        fence = next(iter(md.query(FNDMarkdownFence)))
        content = fence._highlighted_code
        # No exception, text preserved verbatim.
        assert "just plain text 123" in content.plain
