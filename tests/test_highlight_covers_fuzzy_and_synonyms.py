"""Highlights cover every match the cascade would surface — exact
literal stems, fuzzy-AUTO variants, and synonym-expanded forms.

Pre-fix the highlighter only marked words whose Snowball stem matched
the user query stem exactly, so chunks that surfaced via the cascade's
fuzzy-pass ("Templatas" → indexed "templat") or synonym-pass
("k8s" → "kubernetes") had no visible highlight even though those
were the words that triggered the hit. This test pins the new
behaviour through the public ``MatchSpec``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.matching import MatchSpec, word_matches
from fnd.synonyms import SynonymTable
from fnd.tui import FNDApp
from fnd.tui.app import FNDMarkdown

# ── Unit: MatchSpec covers all three pass semantics ──────────────────


def test_match_spec_exact_stem() -> None:
    spec = MatchSpec.from_query("templates")
    assert word_matches("templates", spec)
    assert word_matches("Template", spec)  # stem-equivalent inflection
    assert not word_matches("scaffold", spec)


def test_match_spec_fuzzy_variant() -> None:
    """User typed a typo (Templatas) — the cascade's fuzzy pass would
    have surfaced docs containing "templates" / "template". The spec
    must therefore highlight those words too."""
    spec = MatchSpec.from_query("templatas")
    assert word_matches("templates", spec)
    assert word_matches("Template", spec)


def test_match_spec_two_typo_fuzzy() -> None:
    """Long stems get AUTO distance 2 so 2-edit typos still light up
    the original word."""
    spec = MatchSpec.from_query("tempplatas")
    assert word_matches("templates", spec)


def test_match_spec_synonym_expansion() -> None:
    """A query for "k8s" with a synonym group {k8s, kubernetes}
    highlights both forms — the synonym pass would have surfaced docs
    that only mention "kubernetes"."""
    syns = SynonymTable.from_groups([["k8s", "kubernetes"]])
    spec = MatchSpec.from_query("k8s pod", synonyms=syns)
    assert word_matches("kubernetes", spec)
    assert word_matches("k8s", spec)
    assert word_matches("pod", spec)
    assert not word_matches("docker", spec)


def test_match_spec_short_term_skips_fuzzy() -> None:
    """Stems of 1-2 chars get AUTO distance 0 — typos at that length
    almost always change meaning, and any-1-edit explodes false
    positives. Only exact matches highlight."""
    spec = MatchSpec.from_query("ai")
    assert word_matches("ai", spec)
    assert not word_matches("aa", spec)
    assert not word_matches("ab", spec)


def test_match_spec_empty_query_is_inert() -> None:
    spec = MatchSpec.from_query("")
    assert spec.is_empty
    assert not word_matches("anything", spec)


# ── Char-level alignment for fuzzy hits ──────────────────────────────


def test_word_runs_exact_match_is_one_yellow_run() -> None:
    """Literal stem match → single yellow run covering the whole
    word, no orange overlay (no mismatch information to show)."""
    from fnd.render import HIGHLIGHT_STYLE, MISMATCH_STYLE, word_highlight_runs

    spec = MatchSpec.from_query("templates")
    runs = word_highlight_runs("templates", spec)
    assert runs == [(0, len("templates"), HIGHLIGHT_STYLE)]
    # And no orange anywhere.
    assert all(style != MISMATCH_STYLE for _, _, style in runs)


def test_word_runs_fuzzy_match_marks_only_diverging_char_orange() -> None:
    """User types "Templatas"; the doc word "templates" matches via the
    fuzzy pass. Char-level alignment marks the matching prefix and
    suffix yellow and the one diverging char orange — exactly the
    spec the user asked for."""
    from fnd.render import HIGHLIGHT_STYLE, MISMATCH_STYLE, word_highlight_runs

    spec = MatchSpec.from_query("templatas")
    runs = word_highlight_runs("templates", spec)
    # "templates" indices: 0 1 2 3 4 5 6 7 8
    #                      t e m p l a t e s
    # Aligned to "templatas": match through index 6, sub at 7, match at 8.
    assert runs == [
        (0, 7, HIGHLIGHT_STYLE),  # "templat"
        (7, 8, MISMATCH_STYLE),  # "e" — divergent from query "a"
        (8, 9, HIGHLIGHT_STYLE),  # "s"
    ]


def test_word_runs_synonym_match_is_all_yellow() -> None:
    """A synonym hit has no char-level discrepancy story — the user
    typed "k8s" but a synonym group expanded to "kubernetes". The
    whole synonym word should read as a clean match (yellow) rather
    than mostly-orange because of low char-level overlap."""
    from fnd.render import HIGHLIGHT_STYLE, word_highlight_runs

    syns = SynonymTable.from_groups([["k8s", "kubernetes"]])
    spec = MatchSpec.from_query("k8s", synonyms=syns)
    runs = word_highlight_runs("kubernetes", spec)
    assert runs == [(0, len("kubernetes"), HIGHLIGHT_STYLE)]


def test_word_runs_two_typo_fuzzy_marks_both_diverging_chars() -> None:
    """Long stems get AUTO distance 2. User typo "tempplatas" (extra
    'p' AND 'a' in place of 'e') still surfaces "templates", and
    alignment marks both diverging positions orange."""
    from fnd.render import MISMATCH_STYLE, word_highlight_runs

    spec = MatchSpec.from_query("tempplatas")
    runs = word_highlight_runs("templates", spec)
    # Expect at least one orange run somewhere in the middle/end.
    assert any(style == MISMATCH_STYLE for _, _, style in runs)
    # And the runs collectively cover every char of "templates".
    covered = sum(end - start for start, end, _ in runs)
    assert covered == len("templates")


def test_word_runs_non_match_returns_empty() -> None:
    from fnd.render import word_highlight_runs

    spec = MatchSpec.from_query("templates")
    assert word_highlight_runs("strawberry", spec) == []


def test_word_runs_case_difference_alone_is_not_a_mismatch() -> None:
    """Matching is case-insensitive — "Templates" against query
    "templates" is a literal stem hit, all yellow. No orange even
    though 'T' is uppercase in the doc and lowercase in the query."""
    from fnd.render import HIGHLIGHT_STYLE, MISMATCH_STYLE, word_highlight_runs

    spec = MatchSpec.from_query("templates")
    runs = word_highlight_runs("Templates", spec)
    assert runs == [(0, len("Templates"), HIGHLIGHT_STYLE)]
    assert all(style != MISMATCH_STYLE for _, _, style in runs)


# ── Integration: fuzzy hit chunk shows highlights ────────────────────


def _write(p: Path, body: str) -> None:
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
def fuzzy_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    _write(
        a / "Notes.md",
        "# Patterns\n\nThe templates pattern is described here.\n",
    )
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_fuzzy_match_chunk_highlights_the_actual_word(
    cfg: Config, fuzzy_corpus: Path
) -> None:
    """Query ``Templatas`` (a typo) routes through the cascade fuzzy
    pass and surfaces the chunk that contains ``templates``. The
    preview must highlight the matched word ``templates``, not nothing.
    """
    app = FNDApp(
        index_dir=fuzzy_corpus,
        config=cfg,
        collection="notes",
        initial_query="templatas",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        for _ in range(8):
            await pilot.pause()
        pane = app.query_one("#preview_pane", VerticalScroll)
        md_widgets = list(pane.query(FNDMarkdown))
        assert md_widgets, "expected fuzzy-hit chunk to mount"
        # The chars of the doc word "templates" must be COVERED by
        # highlight spans (collectively — for a fuzzy hit the word
        # is split into yellow / orange runs, so a single span won't
        # span the whole word).
        all_styled_chars: set[int] = set()
        word_lower = "templates"
        word_start = -1
        for md in md_widgets:
            for block in md.query("MarkdownBlock"):
                content = getattr(block, "_content", None)
                if content is None:
                    continue
                idx = content.plain.lower().find(word_lower)
                if idx < 0:
                    continue
                word_start = idx
                for span in content.spans:
                    for i in range(span.start, span.end):
                        all_styled_chars.add(i)
                break
            if word_start >= 0:
                break
        assert word_start >= 0, "expected the chunk to render the word"
        word_indices = set(range(word_start, word_start + len(word_lower)))
        covered = word_indices & all_styled_chars
        assert covered == word_indices, (
            f"every char of the matched word should be highlighted; "
            f"covered={sorted(covered)}, expected={sorted(word_indices)}"
        )
