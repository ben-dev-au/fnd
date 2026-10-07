"""``fnd.display_text``: arbitrary text made safe for a terminal. The ``display_*``
policy fits a fixed-width TUI cell; the ``terminal_*`` policy keeps CLI text
verbatim and shows only what acts on the terminal, as visible escapes."""

from __future__ import annotations

import unicodedata

import pytest

from fnd.display_text import display_block, display_line, terminal_block, terminal_line
from tests import _hostile_text


def test_plain_ascii_is_unchanged() -> None:
    assert display_line("Class HashtableOpen 7.4") == "Class HashtableOpen 7.4"


def test_tab_becomes_a_single_space() -> None:
    # The reported bug: a terminal expands ``\t`` to the next tab stop while
    # Rich measures it as zero cells, so the row over-runs the pane border.
    assert display_line("5.\tExplain what") == "5. Explain what"


def test_newline_and_carriage_return_become_spaces() -> None:
    assert display_line("line one\r\nline two") == "line one  line two"


def test_form_feed_and_vertical_tab_become_spaces() -> None:
    assert display_line("a\x0cb\x0bc") == "a b c"


@pytest.mark.parametrize(
    "raw",
    [
        "　",  # ideographic space (measured 2 cells)
        " ",  # no-break space
        " ",  # em space
        " ",  # line separator
    ],
)
def test_exotic_whitespace_collapses_to_one_plain_space(raw: str) -> None:
    assert display_line(f"a{raw}b") == "a b"


def test_runs_of_plain_spaces_are_preserved() -> None:
    # Labels deliberately separate ``loc`` from ``snippet`` with two spaces;
    # sanitising must be one-for-one, never a collapsing pass.
    assert display_line("p.344  of null") == "p.344  of null"


@pytest.mark.parametrize(
    "bad",
    [
        "​",  # zero-width space
        "‍",  # zero-width joiner
        "­",  # soft hyphen
        "﻿",  # BOM / zero-width no-break space
        "‮",  # right-to-left override (bidi spoofing)
        "\x07",  # bell (C0 control)
        "\x7f",  # delete (C0 control)
        "\x9b",  # C1 control
    ],
)
def test_zero_width_control_and_bidi_chars_are_removed(bad: str) -> None:
    assert display_line(f"ab{bad}cd") == "abcd"


def test_wide_cjk_text_is_kept() -> None:
    # A legitimately wide glyph is measured and rendered at 2 cells alike, so it
    # is border-safe and must survive.
    assert display_line("表\t7") == "表 7"


def test_empty_string() -> None:
    assert display_line("") == ""


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_a_display_line_holds_only_printable_characters_and_spaces(raw: str) -> None:
    shown = display_line(raw)
    assert all(
        ch == " " or not (ch.isspace() or unicodedata.category(ch) in {"Cc", "Cf"}) for ch in shown
    )


def test_terminal_escapes_lose_their_escape_byte() -> None:
    assert display_line("\x1b]52;c;aGVsbG8=\x07clip") == "]52;c;aGVsbG8=clip"


@pytest.mark.parametrize(
    "raw",
    ["a\nb", "a\r\nb", "a\rb", "a\vb", "a\fb", "a\x1cb", "a\x85b", "a\u2028b", "a\u2029b"],
)
def test_a_block_keeps_every_line_break_as_one_newline(raw: str) -> None:
    assert display_block(raw) == "a\nb"


def test_each_line_of_a_block_is_a_display_line() -> None:
    assert display_block("a\tb\nc\x1bd\u200b") == "a b\ncd"


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_a_block_splits_into_lines_the_same_way_everywhere(raw: str) -> None:
    shown = display_block(raw)
    assert shown.splitlines() == shown.split("\n") or shown.endswith("\n")
    assert all(line == display_line(line) for line in shown.split("\n"))


def test_a_block_keeps_a_trailing_line_break() -> None:
    assert display_block("a\n") == "a\n"


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("a\x1b]52;c;aGk=\x07b", "a\\x1b]52;c;aGk=\\x07b"),
        ("a\x9bb", "a\\x9bb"),
        ("file\u202egnp.exe", "file\\u202egnp.exe"),
        ("a\u2066b\u2069", "a\\u2066b\\u2069"),
        ("tag\U000e0041", "tag\\U000e0041"),
    ],
)
def test_terminal_text_shows_what_acts_on_the_terminal_as_escapes(raw: str, shown: str) -> None:
    assert terminal_line(raw) == shown
    assert terminal_block(raw) == shown


@pytest.mark.parametrize(
    "raw",
    ["/data/日本語\u3000メモ.md", "a\u00a0b", "क्\u200dष", "tab\there", "x\u200by", "café [draft]"],
)
def test_terminal_text_keeps_every_printable_character(raw: str) -> None:
    assert terminal_line(raw) == raw


def test_a_terminal_line_shows_its_line_breaks_as_escapes() -> None:
    assert terminal_line("a\nb\r\u2028c") == "a\\nb\\r\\u2028c"


def test_a_terminal_block_keeps_each_line_break_as_one_newline() -> None:
    assert terminal_block("a\r\nb\rc\u2028d\x1be") == "a\nb\nc\nd\\x1be"


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_terminal_text_holds_nothing_that_acts_on_the_terminal(raw: str) -> None:
    for shown in (terminal_line(raw), terminal_block(raw).replace("\n", "")):
        assert all(ch == "\t" or unicodedata.category(ch) != "Cc" for ch in shown)
        assert not set(shown) & set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
