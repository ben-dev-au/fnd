"""FilterSpec: text round-trip, gate composition, scope and unknown policy."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fnd.file_facts import FileFacts
from fnd.filters import FilterSpec, build_gate
from fnd.filters.dimensions import NOTE_KINDS, rule_from_text
from fnd.filters.model import Rule, Unknown
from fnd.fsmeta import FileTimes


def _facts(tmp_path: Path, name: str, body: str = "") -> FileFacts:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return FileFacts(f, root=tmp_path)


class TestValuesTheGrammarCannotQuote:
    """Structured dimensions compile a predicate directly, not via DSL text.

    The grammar has no escape for a quote inside a string literal, so a tag
    like ``don't-index`` rendered to text and re-parsed came back as
    ``dont-index`` — a rule that could never match the real tag.
    """

    def test_a_tag_containing_a_quote_still_matches(self, tmp_path: Path) -> None:
        class _OsTags:
            id = "os"

            def available_on(self, platform: str) -> bool:
                return True

            def read(self, ctx: object) -> frozenset[str]:
                return frozenset({"don't-index"})

        f = tmp_path / "n.md"
        f.write_text("x", encoding="utf-8")
        facts = FileFacts(f, root=tmp_path, tag_providers=[_OsTags()])  # type: ignore[list-item]
        assert build_gate(FilterSpec(exclude_tags=("don't-index",))).passes(facts) is False
        assert build_gate(FilterSpec(exclude_tags=("other",))).passes(facts) is True


class TestOrCaptureRegression:
    """A raw clause containing OR must not capture its neighbours.

    Joining clauses with a bare ``AND`` makes ``kind AND x OR y`` parse as
    ``(kind AND x) OR y``, so a source restricted to PDFs admits everything
    matching ``y``.
    """

    def test_rendered_text_isolates_an_or_clause(self, tmp_path: Path) -> None:
        spec = FilterSpec(kinds=("pdf",), raw=("file.size == 1 OR file.size == 2",))
        gate = build_gate(spec)
        md = _facts(tmp_path, "n.md", "xx")  # size 2, but not a pdf
        assert gate.passes(md) is False

    def test_the_naive_join_would_have_admitted_it(self, tmp_path: Path) -> None:
        """Negative control: the unparenthesised form really is wrong."""
        naive = rule_from_text("file.kind in ['pdf'] AND file.size == 1 OR file.size == 2")
        md = _facts(tmp_path, "n.md", "xx")
        assert naive.passes(md) is True


class TestScope:
    def test_frontmatter_rule_does_not_drop_other_kinds(self, tmp_path: Path) -> None:
        """Strict-null would fail ``Course`` on a PDF; the kind scope spares it."""
        rule = rule_from_text("Course == 'DPwC'", applies_to=NOTE_KINDS)
        assert rule.passes(_facts(tmp_path, "paper.pdf")) is True
        assert rule.passes(_facts(tmp_path, "n.md")) is False

    def test_frontmatter_rule_still_applies_to_notes(self, tmp_path: Path) -> None:
        rule = rule_from_text("Course == 'DPwC'", applies_to=NOTE_KINDS)
        assert rule.passes(_facts(tmp_path, "n.md", "---\nCourse: DPwC\n---\n")) is True

    def test_unscoped_rule_applies_everywhere(self, tmp_path: Path) -> None:
        rule = rule_from_text("Course == 'DPwC'")
        assert rule.passes(_facts(tmp_path, "paper.pdf")) is False


class TestUnknownPolicy:
    def test_unknown_created_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ext4 reports no birth time; dropping on unknown would index nothing."""
        monkeypatch.setattr(
            "fnd.file_facts.read_file_times",
            lambda _p: FileTimes(mtime=1_700_000_000, created=0, inode_changed=1),
        )
        gate = build_gate(FilterSpec(created_after=dt.date(2024, 1, 1)))
        assert gate.passes(_facts(tmp_path, "n.md")) is True

    def test_drop_policy_excludes_on_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "fnd.file_facts.read_file_times",
            lambda _p: FileTimes(mtime=1_700_000_000, created=0, inode_changed=1),
        )
        base = rule_from_text("file.created >= 2024-01-01")
        strict = Rule(
            predicate=base.predicate, text=base.text, facts=base.facts, unknown=Unknown.DROP
        )
        assert strict.passes(_facts(tmp_path, "n.md")) is False


class TestGate:
    def test_empty_gate_admits_everything(self, tmp_path: Path) -> None:
        assert build_gate(FilterSpec()).passes(_facts(tmp_path, "n.md")) is True

    def test_all_rules_must_pass(self, tmp_path: Path) -> None:
        gate = build_gate(FilterSpec(kinds=("md",), max_size=1))
        assert gate.passes(_facts(tmp_path, "n.md", "")) is True
        assert gate.passes(_facts(tmp_path, "n.md", "toolong")) is False

    def test_exclude_tags_drops_a_tagged_note(self, tmp_path: Path) -> None:
        from fnd.tags import FrontmatterTagProvider

        f = tmp_path / "n.md"
        f.write_text("---\ntags: [no_index]\n---\n", encoding="utf-8")
        facts = FileFacts(f, root=tmp_path, tag_providers=[FrontmatterTagProvider()])
        assert build_gate(FilterSpec(exclude_tags=("no_index",))).passes(facts) is False

    def test_exclude_tags_keeps_an_untagged_note(self, tmp_path: Path) -> None:
        from fnd.tags import FrontmatterTagProvider

        f = tmp_path / "n.md"
        f.write_text("---\ntags: [keep]\n---\n", encoding="utf-8")
        facts = FileFacts(f, root=tmp_path, tag_providers=[FrontmatterTagProvider()])
        assert build_gate(FilterSpec(exclude_tags=("no_index",))).passes(facts) is True
