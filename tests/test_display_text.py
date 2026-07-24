"""Unit tests for ``fnd.display_text.sanitise_display_text`` — the shared
guarantee that arbitrary extracted text is safe for single-line, fixed-width
display (results-row labels, CLI snippets)."""

from __future__ import annotations

import pytest

from fnd.display_text import sanitise_display_text


def test_plain_ascii_is_unchanged() -> None:
    assert sanitise_display_text("Class HashtableOpen 7.4") == "Class HashtableOpen 7.4"


def test_tab_becomes_a_single_space() -> None:
    # The reported bug: a terminal expands ``\t`` to the next tab stop while
    # Rich measures it as zero cells, so the row over-runs the pane border.
    assert sanitise_display_text("5.\tExplain what") == "5. Explain what"


def test_newline_and_carriage_return_become_spaces() -> None:
    assert sanitise_display_text("line one\r\nline two") == "line one  line two"


def test_form_feed_and_vertical_tab_become_spaces() -> None:
    assert sanitise_display_text("a\x0cb\x0bc") == "a b c"


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
    assert sanitise_display_text(f"a{raw}b") == "a b"


def test_runs_of_plain_spaces_are_preserved() -> None:
    # Labels deliberately separate ``loc`` from ``snippet`` with two spaces;
    # sanitising must be one-for-one, never a collapsing pass.
    assert sanitise_display_text("p.344  of null") == "p.344  of null"


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
    assert sanitise_display_text(f"ab{bad}cd") == "abcd"


def test_wide_cjk_text_is_kept() -> None:
    # A legitimately wide glyph is measured and rendered at 2 cells alike, so it
    # is border-safe and must survive.
    assert sanitise_display_text("表\t7") == "表 7"


def test_empty_string() -> None:
    assert sanitise_display_text("") == ""
