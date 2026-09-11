"""A line window assumed short lines, and a data dump has one post per line.

`posts.xml` became 364 chunks of 33,000 words: 200 lines each, one whole
StackExchange post per line. Every query scoped to that collection then paid
for every one of those words. A chunk is bounded in characters now as well as
lines, so a long line cannot make a chunk the rest of the system was never
sized for.
"""

from __future__ import annotations

from fnd.extract._text import MAX_WINDOW_CHARS, line_windows


def _windows(text: str) -> list[tuple[int, str]]:
    return list(line_windows(text, max_lines=200, overlap_lines=10))


def test_one_enormous_line_is_split_and_keeps_its_line_number() -> None:
    text = "word " * 30_000  # 150k characters on line 1

    out = _windows(text)

    assert len(out) > 1, "one line became one chunk"
    assert all(len(w) <= MAX_WINDOW_CHARS for _, w in out), max(len(w) for _, w in out)
    assert all(start == 1 for start, _ in out), "a piece of line 1 is still on line 1"
    assert "".join(w for _, w in out).replace(" ", "") == text.replace(" ", ""), "text was lost"


def test_many_long_lines_stay_under_the_budget_with_true_line_numbers() -> None:
    lines = [f"row{i} " + "x" * 3_000 for i in range(200)]  # 600k characters

    out = _windows("\n".join(lines))

    assert all(len(w) <= MAX_WINDOW_CHARS for _, w in out), max(len(w) for _, w in out)
    for start, window in out:
        first = window.split("\n", 1)[0]
        assert first.startswith(f"row{start - 1} "), (start, first[:12])


def test_short_lines_are_windowed_exactly_as_before() -> None:
    """The control: ordinary code and data never reach the budget, so their
    windows, counts and start lines are untouched."""
    text = "\n".join(f"line {i}" for i in range(450))

    out = _windows(text)

    assert [start for start, _ in out] == [1, 191, 381]
    assert out[0][1].splitlines()[0] == "line 0"
    assert len(out[1][1].splitlines()) == 200
