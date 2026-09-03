"""FileFacts: laziness, caching, and the total-``__getitem__`` contract."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fnd.file_facts import RESERVED_FACTS, FileFacts
from fnd.filter_dsl import compile_filter
from fnd.fsmeta import FileTimes
from fnd.tags import FrontmatterTagProvider


class _CountingReader:
    """Spy standing in for the indexer's injected frontmatter reader."""

    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.calls = 0
        self._payload = payload or {}

    def __call__(self, path: Path) -> dict[str, object] | None:
        self.calls += 1
        return dict(self._payload)


def _facts(tmp_path: Path, *, body: str = "", name: str = "note.md", **kw: object) -> FileFacts:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return FileFacts(f, root=tmp_path, **kw)  # type: ignore[arg-type]


class TestLaziness:
    def test_cheap_fact_does_not_read_frontmatter(self, tmp_path: Path) -> None:
        reader = _CountingReader()
        facts = _facts(tmp_path, read_frontmatter=reader)
        assert facts["file.ext"] == ".md"
        assert reader.calls == 0

    def test_frontmatter_key_reads_once(self, tmp_path: Path) -> None:
        reader = _CountingReader({"Course": "DPwC"})
        facts = _facts(tmp_path, read_frontmatter=reader)
        assert facts["Course"] == "DPwC"
        assert facts["Course"] == "DPwC"
        assert reader.calls == 1

    def test_contains_then_getitem_reads_once(self, tmp_path: Path) -> None:
        """``Mapping.__contains__`` falls through to ``__getitem__``; the
        evaluator does both for every field, so the cache is load-bearing."""
        reader = _CountingReader({"Course": "DPwC"})
        facts = _facts(tmp_path, read_frontmatter=reader)
        assert "Course" in facts
        assert facts["Course"] == "DPwC"
        assert reader.calls == 1

    def test_predicate_short_circuits_before_the_expensive_fact(self, tmp_path: Path) -> None:
        reader = _CountingReader({"Course": "DPwC"})
        facts = _facts(tmp_path, name="a.txt", read_frontmatter=reader)
        assert compile_filter("file.ext == '.md' AND Course == 'DPwC'")(facts) is False
        assert reader.calls == 0


class TestTotalGetitem:
    def test_malformed_frontmatter_does_not_escape(self, tmp_path: Path) -> None:
        """A parse error must not abort the index run mid-walk."""

        def boom(path: Path) -> dict[str, object] | None:
            from fnd.frontmatter import FrontmatterParseError

            raise FrontmatterParseError("bad block")

        facts = _facts(tmp_path, read_frontmatter=boom)
        with pytest.raises(KeyError):
            _ = facts["Course"]
        assert compile_filter("Course == 'x'")(facts) is False

    def test_unreadable_file_yields_unknown_size(self, tmp_path: Path) -> None:
        facts = FileFacts(tmp_path / "gone.md", root=tmp_path)
        assert facts.is_unknown("file.size")
        with pytest.raises(KeyError):
            _ = facts["file.size"]

    def test_unknown_is_distinct_from_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ext4 without statx reports created=0; that is unknown, not the epoch."""
        monkeypatch.setattr(
            "fnd.file_facts.read_file_times",
            lambda _p: FileTimes(mtime=1, created=0, inode_changed=1),
        )
        facts = _facts(tmp_path)
        assert facts.is_unknown("file.created") is True
        assert facts.is_unknown("file.modified") is False
        assert facts.is_unknown("Course") is False  # absent, not unknown


class TestFactValues:
    def test_path_is_root_relative_posix(self, tmp_path: Path) -> None:
        nested = tmp_path / "sub" / "deep"
        nested.mkdir(parents=True)
        (nested / "n.md").write_text("", encoding="utf-8")
        facts = FileFacts(nested / "n.md", root=tmp_path)
        assert facts["file.path"] == "sub/deep/n.md"

    def test_kind_and_category(self, tmp_path: Path) -> None:
        facts = _facts(tmp_path)
        assert facts["file.kind"] == "md"
        assert facts["file.category"] == "notes"

    def test_dates_are_date_objects(self, tmp_path: Path) -> None:
        facts = _facts(tmp_path)
        assert isinstance(facts["file.modified"], dt.date)

    def test_tags_are_ordered_tuples_the_dsl_can_search(self, tmp_path: Path) -> None:
        facts = _facts(
            tmp_path,
            body="---\ntags: [no_index, draft]\n---\n",
            read_frontmatter=None,
            tag_providers=[FrontmatterTagProvider()],
        )
        assert facts["file.tags.frontmatter"] == ("draft", "no_index")
        assert compile_filter("'no_index' in file.tags.frontmatter")(facts) is True
        assert compile_filter("'no_index' in file.tags.all")(facts) is True

    def test_hidden_reflects_the_relative_path(self, tmp_path: Path) -> None:
        hidden = tmp_path / ".secret"
        hidden.mkdir()
        (hidden / "n.md").write_text("", encoding="utf-8")
        assert FileFacts(hidden / "n.md", root=tmp_path)["file.hidden"] is True
        assert _facts(tmp_path)["file.hidden"] is False


class TestMappingContract:
    def test_iter_and_len_include_both_namespaces(self, tmp_path: Path) -> None:
        facts = _facts(tmp_path, read_frontmatter=_CountingReader({"Course": "DPwC"}))
        keys = set(facts)
        assert RESERVED_FACTS <= keys
        assert "Course" in keys
        assert len(facts) == len(RESERVED_FACTS) + 1
