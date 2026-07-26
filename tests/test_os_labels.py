"""Per-platform display vocabulary (``fnd.os_labels``).

Sibling of ``tests/test_launcher.py``: the platform is monkeypatched rather
than detected, so every branch runs identically on any host in the CI matrix.
"""

from __future__ import annotations

import pytest

from fnd import os_labels


def _as(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    monkeypatch.setattr("fnd.os_labels.platform.system", lambda: system)


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ("Darwin", "Finder"),
        ("Windows", "File Explorer"),
        ("Linux", "file manager"),
        ("FreeBSD", "file manager"),  # any other POSIX desktop
    ],
)
def test_file_manager_name_per_platform(
    monkeypatch: pytest.MonkeyPatch, system: str, expected: str
) -> None:
    _as(monkeypatch, system)
    assert os_labels.file_manager_name() == expected


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        # Proper nouns take no article; the generic Linux fallback needs one,
        # or help text reads "reveals in file manager".
        ("Darwin", "Finder"),
        ("Windows", "File Explorer"),
        ("Linux", "your file manager"),
        ("FreeBSD", "your file manager"),
    ],
)
def test_file_manager_phrase_adds_an_article_only_where_needed(
    monkeypatch: pytest.MonkeyPatch, system: str, expected: str
) -> None:
    _as(monkeypatch, system)
    assert os_labels.file_manager_phrase() == expected


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ("Darwin", "Reveal in Finder"),
        ("Windows", "Reveal in File Explorer"),
        ("Linux", "Reveal in file manager"),
    ],
)
def test_reveal_label_names_the_native_file_manager(
    monkeypatch: pytest.MonkeyPatch, system: str, expected: str
) -> None:
    _as(monkeypatch, system)
    assert os_labels.reveal_label() == expected


def test_is_macos_only_true_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Darwin")
    assert os_labels.is_macos() is True
    _as(monkeypatch, "Windows")
    assert os_labels.is_macos() is False
    _as(monkeypatch, "Linux")
    assert os_labels.is_macos() is False


def test_alt_modifier_is_the_option_glyph_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS keyboards label the key ⌥, not 'Alt' — the keybindings page and
    the footer both render it, so the vocabulary lives in one place."""
    _as(monkeypatch, "Darwin")
    assert os_labels.modifier_label("alt") == "⌥"
    _as(monkeypatch, "Windows")
    assert os_labels.modifier_label("alt") == "Alt"
    _as(monkeypatch, "Linux")
    assert os_labels.modifier_label("alt") == "Alt"


def test_alt_word_is_option_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prose ('hold Option and arrow') needs the word, not the glyph."""
    _as(monkeypatch, "Darwin")
    assert os_labels.alt_word() == "Option"
    _as(monkeypatch, "Windows")
    assert os_labels.alt_word() == "Alt"


def test_shared_modifiers_are_stable_across_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl and Shift are spelled the same everywhere — only ⌥/⌘ differ, so a
    platform switch must not churn the rest of the key column."""
    for system in ("Darwin", "Windows", "Linux"):
        _as(monkeypatch, system)
        assert os_labels.modifier_label("ctrl") == "Ctrl"
        assert os_labels.modifier_label("shift") == "Shift"


def test_cmd_modifier_is_the_command_glyph_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Darwin")
    assert os_labels.modifier_label("cmd") == "⌘"
    _as(monkeypatch, "Linux")
    assert os_labels.modifier_label("cmd") == "Cmd"


def test_modifier_label_returns_none_for_non_modifiers() -> None:
    """``_pretty_key`` uses the None return to tell a modifier from a key name,
    so it must not fall back to echoing the input."""
    assert os_labels.modifier_label("right") is None
    assert os_labels.modifier_label("q") is None
    assert os_labels.modifier_label("") is None


def test_no_caching_so_the_platform_seam_stays_patchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike ``launcher.get_launcher``, these are uncached pure functions —
    a test (or a same-process platform patch) sees the change with no
    cache_clear() bookkeeping."""
    _as(monkeypatch, "Darwin")
    assert os_labels.file_manager_name() == "Finder"
    _as(monkeypatch, "Windows")
    assert os_labels.file_manager_name() == "File Explorer"
