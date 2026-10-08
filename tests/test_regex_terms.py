"""``fnd.regex_terms``: a user ``/regex/`` matched against words by tantivy's
own engine, so highlighting agrees with search and cannot backtrack forever."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from fnd.regex_terms import matches, matching


@pytest.mark.parametrize(
    ("pattern", "stem", "expected"),
    [
        ("cr[y]pto", "crypto", True),
        ("crypt.*", "cryptographi", True),
        ("crypt.*", "kryptonit", False),
        ("CRYPTO", "crypto", True),
        ("[0-9]{4}", "2024", True),
        ("[0-9]{4}", "202", False),
    ],
)
def test_a_pattern_matches_whole_stems(pattern: str, stem: str, expected: bool) -> None:
    assert matches(pattern, stem) is expected


@pytest.mark.parametrize("pattern", ["(a+)+$", "(?<=a)b", r"(a)\1"])
def test_a_pattern_the_search_engine_refuses_matches_nothing(pattern: str) -> None:
    assert not matches(pattern, "ab")
    assert not matches(pattern, "aa")


def test_a_batch_answers_every_stem() -> None:
    assert matching("crypt.*", {"crypto", "cryptid", "wallet"}) == {"crypto", "cryptid"}


def _finishes(code: str) -> str:
    """``code``'s output, run apart so a backtracking engine fails the test
    by timing out instead of hanging the suite."""
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
    )
    return done.stdout.strip()


def test_a_pathological_pattern_finishes() -> None:
    code = "from fnd.regex_terms import matches; print(matches('(a+)+b', 'a' * 60 + 'c'))"
    assert _finishes(code) == "False"


def test_highlighting_a_pathological_regex_finishes() -> None:
    code = (
        "from fnd.matching import MatchSpec, word_matches;"
        "print(word_matches('a' * 60 + 'c', MatchSpec.from_query('/(a+)+b/')))"
    )
    assert _finishes(code) == "False"


def test_a_batch_that_overflows_the_cache_is_still_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fnd.regex_terms._STEMS_KEPT", 3)
    assert matching("ev.*", {"evict", "evade", "x"}) == {"evict", "evade"}
    assert matching("ev.*", {"evict", "even", "y", "z"}) == {"evict", "even"}


def test_a_known_answer_does_not_rescan_the_cache() -> None:
    pattern = "warm.*"
    matching(pattern, {f"warm{i}" for i in range(20_000)})
    started = time.perf_counter()
    for i in range(2_000):
        matches(pattern, f"warm{i}")
    assert time.perf_counter() - started < 0.5


def test_a_regex_search_runs_the_engine_once_for_all_its_snippets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd import regex_terms
    from fnd.index import build_index
    from fnd.query import Searcher

    docs = tmp_path / "docs"
    docs.mkdir()
    filler = " ".join(f"word{i}" for i in range(60))
    for n in range(12):
        (docs / f"n{n}.md").write_text(f"# N{n}\n\n{filler} cryptography{n} {filler}\n")
    build_index(roots=[docs], index_dir=tmp_path / "idx", collection="c", tag_sources=())
    searcher = Searcher(index_dir=tmp_path / "idx")
    runs: list[int] = []
    real = regex_terms._run

    def _counted(pattern: str, stems: set[str]) -> set[str]:
        runs.append(len(stems))
        return real(pattern, stems)

    monkeypatch.setattr(regex_terms, "_run", _counted)
    hits = searcher._raw_hits("/crypto.*/", limit=20, collection=None)
    assert len(hits) == 12
    assert len(runs) <= 2, runs
