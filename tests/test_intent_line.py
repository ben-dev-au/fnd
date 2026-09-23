"""The ``intent:`` line in the :multi DSL."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def unambiguous_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    _write_md(
        a / "biology-cell-organelles.md",
        "# Cell Organelles\n\n## Mitochondrion\nThe mitochondrion is the powerhouse "
        "of the cell. mitochondrion mitochondrion mitochondrion.\n",
    )
    for i in range(14):
        _write_md(a / f"unrelated-{i:02d}.md", f"# Note {i}\n\nFiller content.\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def test_parse_multi_input_extracts_intent() -> None:
    from fnd.fusion import parse_multi_input

    text = "intent: web page latency\nlex: performance\n"
    result = parse_multi_input(text, synonyms=None)
    assert result.intent == "web page latency"
    assert len(result.subqueries) == 1
    assert result.subqueries[0].source == "lex"


def test_parse_multi_input_no_intent_returns_none() -> None:
    from fnd.fusion import parse_multi_input

    result = parse_multi_input("lex: foo\nphrase: bar baz\n", synonyms=None)
    assert result.intent is None
    assert len(result.subqueries) == 2


def test_parse_multi_input_intent_does_not_become_subquery() -> None:
    from fnd.fusion import parse_multi_input

    result = parse_multi_input("intent: docs\nlex: foo\n", synonyms=None)
    assert all(s.source != "intent" for s in result.subqueries)


def test_parse_multi_input_intent_last_write_wins() -> None:
    """Multiple intent lines are tolerated; the last one wins (matches
    QMD's "at most one intent line" rule)."""
    from fnd.fusion import parse_multi_input

    result = parse_multi_input("intent: first\nlex: x\nintent: second\n", synonyms=None)
    assert result.intent == "second"


def test_make_snippet_prefers_intent_match() -> None:
    """Two query-term occurrences exist; prefer the window that contains
    an intent-token over one that doesn't."""
    from fnd.query import _make_snippet

    body = (
        "Performance is great in athletes. "
        "Athletic performance ranges widely. "
        "Web page performance and load times are critical for UX. "
        "Server-side performance under load."
    )
    snippet_no_intent = _make_snippet(body, "performance", intent=None)
    snippet_web_intent = _make_snippet(body, "performance", intent="web page load")
    # The intent-aware snippet should mention 'web page' or 'load'.
    assert "web page" in snippet_web_intent.lower() or "load" in snippet_web_intent.lower()
    # The intent-less snippet just picks the first match — no constraint
    # on its content beyond containing 'performance'.
    assert "performance" in snippet_no_intent.lower()


def test_make_snippet_proximity_anchors_on_cooccurrence() -> None:
    """A proximity/phrase query must anchor the snippet where its terms
    co-occur (the real match), not on an earlier lone-term hit — and it must
    see through the ``{N}`` / ``"…"~N`` / ``*`` DSL sigils that a raw split misses."""
    from fnd.query import _make_snippet

    body = (
        "Static code analysis runs without executing it. "  # a lone 'code' up front
        + ("filler word " * 30)
        + "Exit codes tell the pipeline how to proceed."  # the genuine 'exit code'
    )
    for q in ("{5}exit code", '"exit code"~5', "{5}exit code*", "exit code"):
        snip = _make_snippet(body, q)
        assert "exit codes" in snip.lower(), f"{q!r} anchored wrong: {snip!r}"


def test_make_snippet_falls_back_when_no_intent_match() -> None:
    """If no occurrence's window overlaps with intent tokens, return the
    first-occurrence snippet (no error, no missed result)."""
    from fnd.query import _make_snippet

    body = "Performance varies. Athletic performance is well-studied."
    # Intent tokens won't appear in the body — fallback to first match.
    snippet = _make_snippet(body, "performance", intent="quantum chromodynamics")
    assert "performance" in snippet.lower()


def test_make_snippet_sanitises_tabs_and_control_chars_at_source() -> None:
    """Extracted PDF body text carries tabs (exercise numbering: ``5.\\tExplain``)
    and stray controls. A snippet is a single-line display string, so the source
    must strip them — the CLI and every label consumer inherit clean text."""
    from fnd.query import _make_snippet

    body = "of null. 5.\tExplain what is wrong\x07 with the hash table strategy"
    snippet = _make_snippet(body, "hash table")
    assert "\t" not in snippet
    assert "\x07" not in snippet
    assert "5. Explain what is wrong" in snippet


def test_intent_in_multi_input_disables_bypass(cfg: Config, unambiguous_index: Path) -> None:
    """End-to-end: parse_multi_input → search_layered with intent →
    regime is NOT strong-signal."""
    from fnd.fusion import parse_multi_input
    from fnd.layered import search_layered
    from fnd.query import Searcher

    parsed = parse_multi_input("intent: organelles\nlex: mitochondrion\n", synonyms=None)
    assert parsed.intent == "organelles"

    searcher = Searcher(index_dir=unambiguous_index)
    _, trace = search_layered(
        searcher,
        query="mitochondrion",
        limit=10,
        sections_per_file=5,
        collection="notes",
        intent=parsed.intent,
        with_trace=True,
    )
    assert trace.regime != "strong-signal"
    assert trace.strong_signal.disabled_by_intent is True
