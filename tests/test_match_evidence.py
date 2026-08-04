"""``match_evidence`` reports whether the preview can show a result's match.

The load-bearing property is what it does NOT do: it never filters. The engine's
match is what makes a result a result; if the highlighter can't paint it, the
row is marked, never withheld — otherwise a highlighting bug would silently
subtract matches the user never learns they missed.
"""

from __future__ import annotations

from fnd.extract.base import Block
from fnd.matching import MatchSpec
from fnd.query import FileChunk, Hit
from fnd.tui.match_evidence import has_paintable_match, rendered_text


def _chunk(*, kind: str = "pdf", body_md: str = "", blocks: list[str] | None = None) -> FileChunk:
    return FileChunk(
        parent_id="p",
        path="/doc.pdf",
        kind=kind,
        page=1,
        slide=0,
        heading_path="Introduction > Assessment Test",
        chunk_seq=1,
        blocks=[Block(kind="p", text=t) for t in (blocks or [])],
        body_md=body_md,
    )


def _hit(*, kind: str = "pdf", body_md: str = "", body_text: str = "") -> Hit:
    return Hit(
        score=1.0,
        parent_id="p",
        path="/doc.pdf",
        kind=kind,
        page=1,
        slide=0,
        heading_path="Introduction > Assessment Test",
        title="",
        snippet="",
        body_text=body_text,
        body_md=body_md,
    )


def test_rendered_text_follows_the_mount_decision() -> None:
    """Whichever substrate the preview would mount is the one we judge."""
    structural = _chunk(body_md="## Assessment Test", blocks=["flat page text"])
    flat = _chunk(body_md="", blocks=["flat page text"])

    assert rendered_text(structural) == "## Assessment Test"
    assert rendered_text(flat) == "flat page text"


def test_hit_and_chunk_answer_identically() -> None:
    """The results pane holds Hits, the preview holds FileChunks; one check
    must serve both or the row marker and the landing disagree."""
    spec = MatchSpec.from_query("test")
    chunk = _chunk(body_md="A. You get service credits", blocks=["Assessment Test", "A. You get"])
    hit = _hit(body_md="A. You get service credits", body_text="Assessment Test\nA. You get")

    assert has_paintable_match(chunk, spec) is has_paintable_match(hit, spec) is False


def test_reports_unlocatable_when_only_the_unrendered_substrate_matches() -> None:
    """The reported bug's shape: the term is in the searchable/flat text but
    not in the markdown the preview renders."""
    spec = MatchSpec.from_query("test")
    chunk = _chunk(body_md="A. You get service credits", blocks=["Assessment Test", "A. You get"])

    assert has_paintable_match(chunk, spec) is False


def test_reports_visible_when_the_rendered_substrate_matches() -> None:
    spec = MatchSpec.from_query("test")
    chunk = _chunk(body_md="## Assessment Test\n\nA. You get", blocks=["Assessment Test"])

    assert has_paintable_match(chunk, spec) is True


def test_stem_variants_count_as_visible() -> None:
    """The highlighter paints "testing" for a query of "test", so evidence
    must agree — otherwise every stemmed hit would be marked unlocatable."""
    spec = MatchSpec.from_query("test")

    assert has_paintable_match(_chunk(body_md="Unit testing matters"), spec) is True


def test_empty_spec_is_never_unlocatable() -> None:
    """Filter-only queries and highlights-off have no match to locate, so
    there is nothing to warn about."""
    chunk = _chunk(body_md="nothing relevant here")

    assert has_paintable_match(chunk, MatchSpec()) is True


def test_module_exposes_no_filter() -> None:
    """A guard on the design, not the behaviour: adding a filtering helper here
    is how the highlighter would quietly become the gate on results."""
    import fnd.tui.match_evidence as module

    assert set(module.__all__) == {"has_paintable_match", "rendered_text"}
    assert not [n for n in dir(module) if "filter" in n.lower() or "drop" in n.lower()]
