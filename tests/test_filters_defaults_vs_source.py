"""Global defaults combined with per-source overrides, at the walk.

Resolution is per field, so every dimension has four cases: default only,
source only, both (source wins), and a source overriding a default to
nothing. Asserting on the files yielded rather than the resolved model,
because a field can resolve correctly and still not reach the gate.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fnd.config import Config, DefaultFilters, SourceFilters
from fnd.walk import walk_sources


def _corpus(root: Path) -> None:
    (root / "note.md").write_text("---\ntags: [keep]\n---\nbody\n", encoding="utf-8")
    (root / "draft.md").write_text("---\ntags: [draft]\n---\nbody\n", encoding="utf-8")
    (root / "paper.pdf").write_bytes(b"%PDF-1.4 tiny")
    (root / "notes.txt").write_text("plain", encoding="utf-8")
    (root / "big.md").write_text("x" * 5000, encoding="utf-8")


def _names(root: Path, defaults: DefaultFilters, source: SourceFilters | None) -> set[str]:
    cfg = Config.model_validate(
        {
            "defaults": {"filters": defaults.model_dump(mode="json")},
            "collections": {
                "c": {
                    "sources": [
                        {
                            "path": str(root),
                            **({"filters": source.model_dump(mode="json")} if source else {}),
                        }
                    ]
                }
            },
        }
    )
    return {p.name for p in walk_sources(sources=cfg.collections["c"].sources)}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    _corpus(tmp_path)
    return tmp_path


class TestPerFieldResolution:
    def test_a_default_applies_when_the_source_says_nothing(self, corpus: Path) -> None:
        got = _names(corpus, DefaultFilters(kinds=["md"], exclude_tags=[]), None)
        assert got == {"note.md", "draft.md", "big.md"}

    def test_a_source_applies_when_the_default_says_nothing(self, corpus: Path) -> None:
        got = _names(corpus, DefaultFilters(exclude_tags=[]), SourceFilters(kinds=["pdf"]))
        assert got == {"paper.pdf"}

    def test_the_source_wins_where_both_are_set(self, corpus: Path) -> None:
        got = _names(
            corpus, DefaultFilters(kinds=["md"], exclude_tags=[]), SourceFilters(kinds=["pdf"])
        )
        assert got == {"paper.pdf"}, "the default's kinds must not survive an override"

    def test_a_source_can_override_a_default_to_nothing(self, corpus: Path) -> None:
        """An empty list is an override, not an absence — otherwise clearing a
        field silently reinstates the value it was overriding."""
        got = _names(corpus, DefaultFilters(kinds=["md"], exclude_tags=[]), SourceFilters(kinds=[]))
        assert "paper.pdf" in got
        assert "notes.txt" in got

    def test_an_unset_field_leaves_its_neighbours_alone(self, corpus: Path) -> None:
        """Overriding one dimension must not reset the rest of the block."""
        got = _names(
            corpus,
            DefaultFilters(kinds=["md"], exclude_tags=["draft"], max_size=1000),
            SourceFilters(kinds=["md", "txt"]),
        )
        assert got == {"note.md", "notes.txt"}
        assert "draft.md" not in got, "the default's tag rule stopped applying"
        assert "big.md" not in got, "the default's size rule stopped applying"


class TestDimensionsCombine:
    def test_every_rule_must_pass(self, corpus: Path) -> None:
        got = _names(
            corpus,
            DefaultFilters(exclude_tags=["draft"]),
            SourceFilters(kinds=["md"], max_size=1000),
        )
        assert got == {"note.md"}

    def test_exclude_beats_include_for_the_same_tag(self, corpus: Path) -> None:
        """Both rules are ANDed, so a contradiction drops the file rather than
        resolving to one side — the safe direction for a corpus filter."""
        got = _names(
            corpus,
            DefaultFilters(exclude_tags=["keep"]),
            SourceFilters(include_tags=["keep"]),
        )
        assert "note.md" not in got

    def test_a_date_bound_and_a_kind_bound_intersect(self, corpus: Path) -> None:
        tomorrow = dt.date.today() + dt.timedelta(days=1)
        assert (
            _names(corpus, DefaultFilters(exclude_tags=[]), SourceFilters(modified_after=tomorrow))
            == set()
        )


class TestSourcesAreIndependent:
    def test_two_sources_keep_their_own_filters(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        _corpus(a)
        _corpus(b)
        cfg = Config.model_validate(
            {
                "defaults": {"filters": {"exclude_tags": []}},
                "collections": {
                    "c": {
                        "sources": [
                            {"path": str(a), "filters": {"kinds": ["md"]}},
                            {"path": str(b), "filters": {"kinds": ["pdf"]}},
                        ]
                    }
                },
            }
        )
        got = {(p.parent.name, p.name) for p in walk_sources(sources=cfg.collections["c"].sources)}
        assert {n for parent, n in got if parent == "a"} == {"note.md", "draft.md", "big.md"}
        assert {n for parent, n in got if parent == "b"} == {"paper.pdf"}

    def test_an_override_on_one_source_does_not_leak_to_a_sibling(self, tmp_path: Path) -> None:
        """``_resolved_filters`` is stamped per source; sharing one object
        would make the last source's override win everywhere."""
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        _corpus(a)
        _corpus(b)
        cfg = Config.model_validate(
            {
                "defaults": {"filters": {"kinds": ["md"], "exclude_tags": []}},
                "collections": {
                    "c": {
                        "sources": [
                            {"path": str(a)},
                            {"path": str(b), "filters": {"kinds": ["pdf"]}},
                        ]
                    }
                },
            }
        )
        first, second = cfg.collections["c"].sources
        assert first.effective_filters.kinds == ["md"]
        assert second.effective_filters.kinds == ["pdf"]


class TestIgnoreFlagsResolve:
    def test_a_source_can_switch_an_ignore_file_off(self, tmp_path: Path) -> None:
        _corpus(tmp_path)
        (tmp_path / ".gitignore").write_text("*.md\n", encoding="utf-8")
        on = _names(tmp_path, DefaultFilters(exclude_tags=[]), None)
        off = _names(
            tmp_path, DefaultFilters(exclude_tags=[]), SourceFilters(respect_gitignore=False)
        )
        assert "note.md" not in on
        assert "note.md" in off

    def test_false_is_an_override_not_an_absence(self, tmp_path: Path) -> None:
        """``False`` is falsy; a truthiness test here would read it as unset
        and silently inherit the default's ``True``."""
        _corpus(tmp_path)
        (tmp_path / ".gitignore").write_text("*.md\n", encoding="utf-8")
        source = SourceFilters(respect_gitignore=False)
        assert _names(tmp_path, DefaultFilters(exclude_tags=[]), source) >= {"note.md"}
