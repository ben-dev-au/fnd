"""Proximity-constrained highlighting: only occurrences inside a qualifying
co-occurrence window render at full strength; the rest are dimmed."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from rich.text import Text
from textual.content import Span

from fnd.matching import MatchSpec, _stem, proximity_qualifying_indices


def _stems(*words: str) -> tuple[str, ...]:
    return tuple(_stem(w) for w in words)


def _tokens(index_to_stem: dict[int, str], length: int) -> list[str]:
    """Build a stems-by-token list of ``length`` filler tokens with the given
    stems planted at specific indices."""
    out = ["x"] * length
    for i, s in index_to_stem.items():
        out[i] = s
    return out


def test_cluster_qualifies_isolated_does_not():
    # vuln/threat/risk cluster at 1,3,5 (span 4 <= 5+2); a lone vuln far away.
    toks = _tokens({1: "vuln", 3: "threat", 5: "risk", 50: "vuln"}, 51)
    group = (("vuln", "threat", "risk"), 5)
    assert proximity_qualifying_indices(toks, group) == {1, 3, 5}


def test_reordered_terms_still_qualify():
    # span-window ignores order: risk/threat/vuln at 0,1,2, slop 0 -> span 2 <= 2.
    toks = _tokens({0: "risk", 1: "threat", 2: "vuln"}, 3)
    group = (("vuln", "threat", "risk"), 0)
    assert proximity_qualifying_indices(toks, group) == {0, 1, 2}


def test_at_bound_qualifies():
    # a@0, b@1, c@7 -> span 7 == slop(5)+2. Exactly at the bound.
    toks = _tokens({0: "a", 1: "b", 7: "c"}, 8)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 5)) == {0, 1, 7}


def test_one_past_bound_does_not_qualify():
    # a@0, b@1, c@8 -> span 8 > slop(5)+2 = 7.
    toks = _tokens({0: "a", 1: "b", 8: "c"}, 9)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 5)) == set()


def test_repeated_term_only_included_when_in_window():
    # a@0, a@5, b@6, c@7, slop 0 -> bound 2. Window [5,7] covers a@5,b,c (span 2);
    # a@0 is too far (any window with a@0 + b + c spans >= 7) -> excluded.
    toks = _tokens({0: "a", 5: "a", 6: "b", 7: "c"}, 8)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 0)) == {5, 6, 7}


def test_repeated_term_included_when_window_widens():
    # Same layout, slop 5 -> bound 7. Window [0,7] (span 7) covers all incl a@0.
    toks = _tokens({0: "a", 5: "a", 6: "b", 7: "c"}, 8)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 5)) == {0, 5, 6, 7}


def test_missing_term_yields_nothing():
    toks = _tokens({0: "a", 1: "b"}, 5)  # no "c" anywhere
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 5)) == set()


def test_empty_group_yields_nothing():
    toks = _tokens({0: "a"}, 3)
    assert proximity_qualifying_indices(toks, ((), 5)) == set()


def test_two_clusters_both_qualify():
    toks = _tokens({0: "a", 1: "b", 2: "c", 40: "c", 41: "b", 42: "a"}, 43)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 1)) == {0, 1, 2, 40, 41, 42}


# --- MatchSpec.proximity_groups -------------------------------------------


def test_brace_proximity_populates_group():
    spec = MatchSpec.from_query("{5}vulnerability threat risk")
    assert spec.proximity_groups == ((_stems("vulnerability", "threat", "risk"), 5),)
    # Group terms still drive ordinary word matching.
    assert _stems("vulnerability", "threat", "risk")[0] in spec.exact_stems


def test_near_proximity_populates_group():
    spec = MatchSpec.from_query("malware NEAR/3 detection")
    assert spec.proximity_groups == ((_stems("malware", "detection"), 3),)
    assert set(_stems("malware", "detection")) <= spec.exact_stems


def test_typed_phrase_slop_is_proximity_not_contiguous():
    spec = MatchSpec.from_query('"climate change"~4')
    assert spec.proximity_groups == ((_stems("climate", "change"), 4),)
    # Must NOT also be treated as a contiguous (slop-0) phrase.
    assert _stems("climate", "change") not in spec.phrases
    assert set(_stems("climate", "change")) <= spec.exact_stems


def test_plain_query_has_no_proximity_groups():
    spec = MatchSpec.from_query("vulnerability threat risk")
    assert spec.proximity_groups == ()


def test_quoted_phrase_without_slop_stays_contiguous():
    spec = MatchSpec.from_query('"climate change"')
    assert spec.proximity_groups == ()
    assert _stems("climate", "change") in spec.phrases


def test_standalone_phrase_survives_alongside_same_terms_proximity():
    # A standalone contiguous phrase and a proximity group sharing its terms must
    # coexist: the unsloped phrase stays contiguous, the sloped one is a group.
    spec = MatchSpec.from_query('"climate change" "climate change"~4')
    assert spec.proximity_groups == ((_stems("climate", "change"), 4),)
    assert _stems("climate", "change") in spec.phrases


def test_slop_zero_phrase_stays_contiguous_not_proximity():
    # "a b"~0 is slop 0 == contiguous: no proximity group, still a phrase.
    spec = MatchSpec.from_query('"climate change"~0')
    assert spec.proximity_groups == ()
    assert _stems("climate", "change") in spec.phrases


def test_proximity_only_spec_is_not_empty():
    # is_empty must agree with from_query's guard, which counts proximity_groups.
    assert not MatchSpec.from_query("{3}vulnerability threat risk").is_empty
    assert not MatchSpec(proximity_groups=((("a", "b"), 3),)).is_empty


def test_brace_alias_does_not_leak_digit_into_colour_order():
    # The {N} digit must not consume a colour slot: the first real term gets
    # slot 0 (yellow), matching a typed proximity query.
    from fnd.matching import match_color

    spec = MatchSpec.from_query("{4}vulnerability threat risk")
    assert match_color("vulnerability", spec) == 0


# --- dim style variants ----------------------------------------------------


def test_match_style_dim_differs_from_full():
    from fnd.render import match_style

    assert match_style(0, dim=True) != match_style(0)
    assert match_style(1, dim=True) != match_style(1)


def test_word_highlight_runs_dim_uses_dim_style():
    from fnd.render import match_style, word_highlight_runs

    spec = MatchSpec.from_query("vulnerability")
    full = word_highlight_runs("vulnerability", spec, dim=False)
    dim = word_highlight_runs("vulnerability", spec, dim=True)
    assert full
    assert dim
    full_styles = {style for _, _, style in full}
    dim_styles = {style for _, _, style in dim}
    assert full_styles != dim_styles
    assert match_style(0, dim=True) in dim_styles


# --- two-tier wiring in apply_match_highlights -----------------------------


def _styles_at(t: Text, offset: int) -> set[str]:
    return {str(s.style) for s in t.spans if s.start <= offset < s.end}


def test_proximity_cluster_full_lone_occurrence_dim():

    from fnd.render import DIM_MATCH_STYLES, MATCH_STYLES, apply_match_highlights

    line = "vulnerability threat risk " + ("filler " * 20) + "vulnerability"
    spec = MatchSpec.from_query("{3}vulnerability threat risk", auto_fuzzy=False)
    t = Text(line)
    assert apply_match_highlights(t, spec)

    cluster_off = line.index("vulnerability")
    lone_off = line.rindex("vulnerability")
    assert cluster_off != lone_off
    # In-cluster occurrence renders at full strength...
    assert _styles_at(t, cluster_off) & set(MATCH_STYLES)
    assert not (_styles_at(t, cluster_off) & set(DIM_MATCH_STYLES))
    # ...the far-away lone occurrence is dimmed.
    assert _styles_at(t, lone_off) & set(DIM_MATCH_STYLES)
    assert not (_styles_at(t, lone_off) & set(MATCH_STYLES))


def test_plain_query_never_dims():

    from fnd.render import DIM_MATCH_STYLES, apply_match_highlights

    line = "vulnerability here and vulnerability there far apart " + ("x " * 30) + "vulnerability"
    spec = MatchSpec.from_query("vulnerability", auto_fuzzy=False)
    t = Text(line)
    assert apply_match_highlights(t, spec)
    for off in (line.index("vulnerability"), line.rindex("vulnerability")):
        assert not (_styles_at(t, off) & set(DIM_MATCH_STYLES))


# --- LIVE preview path (the markdown widget baker, not the export path) -----


def _span_styles_at(spans: list[Span], offset: int) -> set[str]:
    return {str(s.style) for s in spans if s.start <= offset < s.end}


def test_live_markdown_baker_dims_lone_occurrence():
    # The stock FNDMarkdown widget bakes highlights via _build_match_spans —
    # this is the path the live preview actually uses, NOT apply_match_highlights.
    from fnd.render import DIM_MATCH_STYLES, MATCH_STYLES
    from fnd.tui.widgets.markdown import _build_match_spans

    plain = "vulnerability threat risk " + ("filler " * 20) + "vulnerability"
    spec = MatchSpec.from_query("{3}vulnerability threat risk", auto_fuzzy=False)
    spans = _build_match_spans(plain, spec)

    cluster_off = plain.index("vulnerability")
    lone_off = plain.rindex("vulnerability")
    assert _span_styles_at(spans, cluster_off) & set(MATCH_STYLES)
    assert _span_styles_at(spans, lone_off) & set(DIM_MATCH_STYLES)
    assert not (_span_styles_at(spans, lone_off) & set(MATCH_STYLES))


# --- Auto-scroll target: prefer the co-occurrence, not a dimmed stray --------
# The structural (FNDMarkdown) preview path — used for MD/DOCX/PPTX — picks the
# scroll target via _record_first_match (blocks) and _find_first_match_coord_in_table
# (tables). #77 fixed the equivalent target in the flat (PDF/TXT) line_buffer path
# but not these, so a proximity query landed on the first dimmed lone-term hit.


def test_table_coord_prefers_proximity_cooccurrence():
    # Early cell has a lone "code" (dimmed under {5}exit code); a later cell holds
    # the real "exit code" co-occurrence. The scroll coord must point at the
    # co-occurrence cell, not the dimmed lone-term cell above it.
    from fnd.tui.widgets.markdown import _find_first_match_coord_in_table

    spec = MatchSpec.from_query("{5}exit code", auto_fuzzy=False)
    headers = [Text("Topic"), Text("Notes")]
    rows = [
        [Text("intro"), Text("the code sample")],  # lone "code" -> dim only
        [Text("errors"), Text("returns exit code 1")],  # co-occurrence -> full
    ]
    assert _find_first_match_coord_in_table(headers, rows, spec) == ((1, 1), True)


def test_table_coord_falls_back_to_dim_when_no_cooccurrence():
    # No cell holds both terms together, so the only matches are dimmed strays.
    # We still anchor on the first matching cell rather than returning nothing.
    from fnd.tui.widgets.markdown import _find_first_match_coord_in_table

    spec = MatchSpec.from_query("{5}exit code", auto_fuzzy=False)
    headers = [Text("Topic"), Text("Notes")]
    rows = [
        [Text("intro"), Text("the code sample")],  # lone "code"
        [Text("errors"), Text("clean exit path")],  # lone "exit"
    ]
    assert _find_first_match_coord_in_table(headers, rows, spec) == ((0, 1), False)


@pytest.mark.asyncio
async def test_first_match_block_prefers_proximity_cooccurrence():
    # Paragraph one mentions only "code" (dimmed); the last paragraph carries the
    # real "exit code" co-occurrence. first_match_block must resolve to the
    # co-occurrence paragraph so the preview scrolls to the genuine match.
    # The filler paragraph is load-bearing: the window is chunk-scoped, so
    # without it the lone "code" would sit within slop of the later "exit" and
    # legitimately qualify.
    from textual.app import App, ComposeResult

    from fnd.tui.widgets.markdown import FNDMarkdown

    spec = MatchSpec.from_query("{5}exit code", auto_fuzzy=False)

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield FNDMarkdown(match_spec=spec)

    async with _Harness().run_test() as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.update(
            "para one mentions code only.\n\n"
            "some unrelated filler text goes here to push the paragraphs apart.\n\n"
            "later the exit code is shown.\n"
        )
        await md.build_done.wait()
        fm = md.first_match_block
        assert fm is not None
        assert "exit code" in fm._content.plain


# ── chunk-scoped window: a co-occurrence may straddle a block ───────


def test_multi_single_segment_matches_the_single_segment_form():
    # match_word_spans is now a wrapper over match_word_spans_multi; the two
    # must not drift, for proximity and plain specs alike.
    from fnd.render import match_word_spans, match_word_spans_multi

    text = "A responsive form validation grid for mobile bootstrap layouts."
    for query in ("responsive mobile", "{50}responsive mobile", "bootstrap", "{6}respons* mobile"):
        spec = MatchSpec.from_query(query, auto_fuzzy=False)
        assert match_word_spans(text, spec) == match_word_spans_multi((text,), spec)[0], query


def test_window_spans_a_segment_boundary():
    # The reported bug: "responsive" closes one block and "Mobile" opens the
    # next. Scoped per block, neither could qualify at any slop.
    from fnd.render import DIM_STYLES, match_word_spans_multi

    segments = [
        "His combined example is the standard responsive pattern:",
        "Mobile - one column, full width.",
    ]
    spec = MatchSpec.from_query("{50}responsive mobile", auto_fuzzy=False)
    runs = match_word_spans_multi(segments, spec)
    assert [len(r) for r in runs] == [1, 1]
    assert all(style not in DIM_STYLES for seg in runs for _a, _b, style in seg)


def test_window_bound_still_honoured_across_segments():
    # Widening the scope must not widen the window: push the pair past
    # slop + (n - 1) and both fall back to dimmed.
    from fnd.render import DIM_STYLES, match_word_spans_multi

    segments = ["responsive " + "filler " * 20, "mobile here"]
    spec = MatchSpec.from_query("{5}responsive mobile", auto_fuzzy=False)
    runs = match_word_spans_multi(segments, spec)
    assert [len(r) for r in runs] == [1, 1]
    assert all(style in DIM_STYLES for seg in runs for _a, _b, style in seg)


def test_plain_query_multi_is_per_segment_unchanged():
    from fnd.render import match_word_spans, match_word_spans_multi

    segments = ["a responsive layout", "a mobile layout"]
    spec = MatchSpec.from_query("responsive mobile", auto_fuzzy=False)
    assert match_word_spans_multi(segments, spec) == [match_word_spans(s, spec) for s in segments]


def _painted(text: str, spec: MatchSpec) -> dict[str, bool]:
    """``{covered word: is_full}`` for ``text``. Keyed by the whole doc word, so a
    wildcard hit split into literal/fill runs collapses back to one entry —
    letting a test name the term it cares about instead of trusting that "some
    run was full" refers to the right one."""
    from fnd.matching import DOC_WORD_RE
    from fnd.render import DIM_STYLES, match_word_spans

    out: dict[str, bool] = {}
    for a, _b, style in match_word_spans(text, spec):
        word = next(m.group(0) for m in DOC_WORD_RE.finditer(text) if m.start() <= a < m.end())
        out[word] = out.get(word, True) and style not in DIM_STYLES
    return out


def test_wildcard_member_can_qualify_a_window():
    # DOC_WORD_RE dropped the glob in BOTH sinks: ``respons*`` reduced to the
    # stem ``respon`` (which no document token carries, so the window never
    # qualified) and left spec.wildcards empty (so the group's own term went
    # unpainted). Assert on the wildcard term by name — asserting only that
    # "some run is full" would pass on the sibling ``mobile`` alone.
    spec = MatchSpec.from_query("{6}respons* mobile", auto_fuzzy=False)
    assert spec.proximity_groups == ((("respons*", "mobil"), 6),)
    assert spec.wildcards == ("respons*",)
    assert _painted("The responsive grid is mobile first.", spec) == {
        "responsive": True,
        "mobile": True,
    }


def test_wildcard_member_outside_the_window_dims():
    # The mirror of the above: a glob member still has to obey the window, or
    # "wildcards qualify" would just mean "wildcards never dim".
    spec = MatchSpec.from_query("{2}respons* mobile", auto_fuzzy=False)
    painted = _painted("The responsive grid " + ("filler " * 20) + "is mobile.", spec)
    assert painted == {"responsive": False, "mobile": False}


def test_repeated_wildcard_member_still_qualifies():
    # ``n`` is the count of DISTINCT members. Deriving it from the raw member
    # list made ``{5}respons* respons* mobile`` demand three distinct members
    # from a two-member group, so nothing could ever qualify.
    spec = MatchSpec.from_query("{5}respons* respons* mobile", auto_fuzzy=False)
    assert _painted("The responsive grid is mobile first.", spec) == {
        "responsive": True,
        "mobile": True,
    }


def test_render_chunk_pieces_window_spans_lines():
    # The plain mount highlighted per source line, so a cross-line window could
    # never qualify there either.
    from fnd.extract.base import Block
    from fnd.query import FileChunk
    from fnd.render import DIM_STYLES, render_chunk_pieces

    chunk = FileChunk(
        parent_id="x",
        path="/x.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="",
        chunk_seq=0,
        blocks=[
            Block(kind="p", text="the standard responsive pattern"),
            Block(kind="ul", text="Mobile only"),
        ],
    )
    spec = MatchSpec.from_query("{50}responsive mobile", auto_fuzzy=False)
    _header, pieces = render_chunk_pieces(chunk, match_spec=spec)
    styles = [str(span.style) for text, _hit in pieces for span in text.spans]
    assert styles, "both terms should paint"
    assert all(style not in DIM_STYLES for style in styles)


@pytest.mark.asyncio
async def test_live_markdown_window_spans_paragraph_and_list_item():
    # End-to-end regression for the reported bug, through the real widget: a
    # paragraph followed by a bullet list, which Textual renders as separate
    # blocks (the item's text lands on an inner paragraph).
    from textual.app import App, ComposeResult
    from textual.widgets._markdown import MarkdownBlock

    from fnd.render import DIM_STYLES
    from fnd.tui.widgets.markdown import FNDMarkdown

    spec = MatchSpec.from_query("{50}responsive mobile", auto_fuzzy=False)

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield FNDMarkdown(match_spec=spec)

    async with _Harness().run_test() as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.update(
            "His combined example is the standard responsive pattern:\n\n"
            "- Mobile - one column, full width.\n"
        )
        await md.build_done.wait()
        painted = [
            (blk._content.plain[s.start : s.end], str(s.style))
            for blk in md.query(MarkdownBlock)
            for s in (getattr(blk, "_fnd_match_spans", None) or [])
        ]
        assert sorted(word for word, _ in painted) == ["Mobile", "responsive"]
        assert all(style not in DIM_STYLES for _, style in painted)
        assert md.first_match_block is not None


@pytest.mark.asyncio
async def test_fence_highlight_survives_style_update():
    # notify_style_update rebuilds _highlighted_code from scratch, dropping every
    # span. Without the cached-span replay the fence would recompute
    # block-locally and silently undo the chunk-wide scope.
    from textual.app import App, ComposeResult

    from fnd.render import MATCH_STYLES
    from fnd.tui.widgets.markdown import FNDMarkdown, FNDMarkdownFence

    spec = MatchSpec.from_query("responsive", auto_fuzzy=False)

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield FNDMarkdown(match_spec=spec)

    async with _Harness().run_test() as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.update("intro\n\n```css\n.responsive { width: 100%; }\n```\n")
        await md.build_done.wait()
        fence = md.query_one(FNDMarkdownFence)

        def _match_spans():
            # Filter on the match PALETTE, not merely on covering the word: the
            # lexer also emits a syntax span over `.responsive`, so a text-only
            # filter would stay green even if the overlay were dropped entirely.
            plain = fence._highlighted_code.plain
            return [
                s
                for s in fence._highlighted_code.spans
                if str(s.style) in set(MATCH_STYLES) and plain[s.start : s.end] == "responsive"
            ]

        assert _match_spans(), "fence should carry a match span"
        fence.notify_style_update()
        assert _match_spans(), "match span must survive the theme rebuild"


@pytest.mark.asyncio
async def test_no_block_owns_both_text_and_children():
    # ``_content_blocks`` yields a block's children INSTEAD of the block when it
    # has any, which is only lossless because Textual's container blocks (lists,
    # table wrappers) own no text of their own. Pin that invariant against a
    # representative tree so a framework change fails here rather than silently
    # dropping text out of the chunk-wide proximity window.
    from textual.app import App, ComposeResult
    from textual.widgets._markdown import MarkdownBlock

    from fnd.tui.widgets.markdown import FNDMarkdown, _block_plain

    def _walk(block: MarkdownBlock) -> Iterator[MarkdownBlock]:
        yield block
        for child in getattr(block, "_blocks", None) or []:
            yield from _walk(child)

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield FNDMarkdown(match_spec=MatchSpec.from_query("alpha", auto_fuzzy=False))

    async with _Harness().run_test() as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.update(
            "# Heading alpha\n\n"
            "Paragraph alpha.\n\n"
            "> Quoted alpha\n\n"
            "- item alpha\n"
            "  - nested alpha\n\n"
            "1. ordered alpha\n\n"
            "| H1 | H2 |\n| --- | --- |\n| c1 alpha | c2 |\n\n"
            "```py\nalpha = 1\n```\n"
        )
        await md.build_done.wait()
        seen: dict[int, object] = {}
        for top in md.query(MarkdownBlock):
            for b in _walk(top):
                seen[id(b)] = b
        assert len(seen) > 10, "expected a representative block tree"
        offenders = [
            type(b).__name__
            for b in seen.values()
            if getattr(b, "_blocks", None) and _block_plain(b)  # type: ignore[arg-type]
        ]
        assert offenders == [], f"container blocks must own no text: {offenders}"
