"""The Keybindings page (and the footer hint bar) speak the running OS.

Before this, the cheat sheet hardcoded macOS: "Reveal in Finder", the ⌥ glyph,
"hold Option", an Apple-Terminal tip, and a macOS-only Accessibility section —
all shown verbatim to Linux and Windows users after the cross-platform port.

Help text carries ``{file_manager}`` / ``{alt_key}`` / ``{alt_word}``
placeholders that ``os_labels.localise`` resolves at *render* time, so these
tests monkeypatch the platform and assert the rendered rows directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from fnd import os_labels
from fnd.tui import FNDApp
from fnd.tui.app import render_hint_bar
from fnd.tui.menu import MenuItem, _pretty_key, _provider_keybindings


def _fake_app() -> FNDApp:
    return cast(FNDApp, SimpleNamespace())


def _as(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    monkeypatch.setattr("fnd.os_labels.platform.system", lambda: system)


def _headers(items: tuple[MenuItem, ...]) -> list[str]:
    return [it.label for it in items if it.is_header]


def _rows(items: tuple[MenuItem, ...]) -> list[MenuItem]:
    return [it for it in items if not it.is_header]


def _page_text(items: tuple[MenuItem, ...]) -> str:
    """Every rendered string on the page, for absence assertions."""
    return "\n".join(f"{it.key} {it.label} {it.description}" for it in items)


# ── Placeholder resolution ───────────────────────────────────────────────


def test_localise_resolves_every_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Windows")
    got = os_labels.localise("{reveal_label}: open {file_manager} with {alt_word} ({alt_key})")
    assert got == "Reveal in File Explorer: open File Explorer with Alt (Alt)"


def test_localise_uses_the_sentence_form_for_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    """``{file_manager}`` sits mid-sentence, so it resolves to the phrase form;
    ``{reveal_label}`` is a row title and stays article-free."""
    _as(monkeypatch, "Linux")
    assert os_labels.localise("reveals in {file_manager}") == "reveals in your file manager"
    assert os_labels.localise("{reveal_label}") == "Reveal in file manager"


def test_localise_is_a_no_op_without_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Darwin")
    assert os_labels.localise("Quit fnd.") == "Quit fnd."


def test_localise_leaves_unrelated_braces_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Query-syntax help mentions `{60}` proximity and `{path}` app templates —
    substitution must be literal token replacement, not str.format."""
    _as(monkeypatch, "Darwin")
    text = "Proximity {60} and argv template {path} stay literal"
    assert os_labels.localise(text) == text


# ── Reveal row naming ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("system", "expected"),
    [("Darwin", "Finder"), ("Windows", "File Explorer"), ("Linux", "file manager")],
)
def test_settings_reveal_row_names_the_native_file_manager(
    monkeypatch: pytest.MonkeyPatch, system: str, expected: str
) -> None:
    _as(monkeypatch, system)
    items = _provider_keybindings(_fake_app())
    reveal_rows = [r for r in _rows(items) if r.key == "Shift+Enter"]
    assert reveal_rows, [r.key for r in _rows(items)]
    row = reveal_rows[0]
    assert row.label == f"Reveal in {expected}"
    assert expected in row.description


def test_no_finder_reference_survives_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Windows")
    assert "Finder" not in _page_text(_provider_keybindings(_fake_app()))


def test_no_finder_reference_survives_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Linux")
    assert "Finder" not in _page_text(_provider_keybindings(_fake_app()))


# ── Modifier vocabulary ──────────────────────────────────────────────────


def test_alt_bindings_render_the_option_glyph_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Darwin")
    assert _pretty_key("ctrl+right,alt+right") == "Ctrl+→ / ⌥+→"


def test_alt_bindings_render_as_alt_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Windows")
    assert _pretty_key("ctrl+right,alt+right") == "Ctrl+→ / Alt+→"


def test_skim_row_uses_the_platform_modifier(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Linux")
    items = _provider_keybindings(_fake_app())
    skim = [r for r in _rows(items) if r.label.startswith("Skim")]
    assert skim, [r.label for r in _rows(items)]
    assert "⌥" not in skim[0].key, skim[0].key
    assert "Alt" in skim[0].key, skim[0].key
    assert "Option" not in skim[0].description


def test_skim_row_keeps_the_option_glyph_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Darwin")
    items = _provider_keybindings(_fake_app())
    skim = next(r for r in _rows(items) if r.label.startswith("Skim"))
    assert "⌥" in skim.key
    assert "Option" in skim.description


def test_apple_terminal_tip_is_macos_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Left-Option-key → Esc+ workaround is a Terminal.app setting; on
    Linux/Windows it's noise pointing at a menu that doesn't exist."""
    _as(monkeypatch, "Darwin")
    assert "Apple Terminal" in _page_text(_provider_keybindings(_fake_app()))
    _as(monkeypatch, "Windows")
    assert "Apple Terminal" not in _page_text(_provider_keybindings(_fake_app()))
    _as(monkeypatch, "Linux")
    assert "Apple Terminal" not in _page_text(_provider_keybindings(_fake_app()))


# ── macOS-only sections ──────────────────────────────────────────────────


def test_accessibility_section_shown_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Darwin")
    assert "Accessibility prompt" in _headers(_provider_keybindings(_fake_app()))


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_accessibility_section_hidden_off_macos(
    monkeypatch: pytest.MonkeyPatch, system: str
) -> None:
    """AX permission gates the macOS Preview AppleScript page-jump; the modal
    can never appear elsewhere, so its keys are dead weight on the page."""
    _as(monkeypatch, system)
    items = _provider_keybindings(_fake_app())
    assert "Accessibility prompt" not in _headers(items)
    assert "System Settings" not in _page_text(items)


def test_hiding_a_section_leaves_the_rest_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Windows")
    headers = _headers(_provider_keybindings(_fake_app()))
    for needed in ("Global", "Settings menu", "Source form", "Open with… modal"):
        assert needed in headers, headers


def test_accessibility_context_hint_degrades_off_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`?` from the AX modal passes that hint; off macOS the section is gone
    and the page must still render rather than KeyError."""
    _as(monkeypatch, "Windows")
    headers = _headers(_provider_keybindings(_fake_app(), context_hint="Accessibility prompt"))
    assert headers[0] == "Global"


# ── Footer hint bar ──────────────────────────────────────────────────────


def test_footer_hints_use_the_platform_modifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """The footer and the cheat sheet must agree — one renderer, one
    vocabulary, so a Windows user isn't told to press ⌥."""
    _as(monkeypatch, "Darwin")
    assert "⌥↑↓" in render_hint_bar((), (("{alt_key}↑↓", "Skim"),)).plain
    _as(monkeypatch, "Windows")
    rendered = render_hint_bar((), (("{alt_key}↑↓", "Skim"),)).plain
    assert "Alt↑↓" in rendered
    assert "⌥" not in rendered


def test_footer_hint_labels_are_localised(monkeypatch: pytest.MonkeyPatch) -> None:
    _as(monkeypatch, "Linux")
    assert "file manager" in render_hint_bar((), (("R", "Reveal in {file_manager}"),)).plain


def test_results_footer_table_has_no_baked_in_glyph() -> None:
    """The table must hold the placeholder, not ⌥ — otherwise the localise
    call in the renderer has nothing to work with."""
    results = FNDApp._FOOTER_CONTEXTUAL["results"]
    keys = [k for k, _ in results]
    assert "⌥↑↓" not in keys, keys
    assert "{alt_key}↑↓" in keys, keys


# ── Every row stays renderable ───────────────────────────────────────────


@pytest.mark.parametrize("system", ["Darwin", "Windows", "Linux"])
def test_no_unresolved_placeholder_reaches_the_user(
    monkeypatch: pytest.MonkeyPatch, system: str
) -> None:
    _as(monkeypatch, system)
    text = _page_text(_provider_keybindings(_fake_app()))
    for token in (
        os_labels.FILE_MANAGER,
        os_labels.REVEAL_LABEL,
        os_labels.ALT_KEY,
        os_labels.ALT_WORD,
    ):
        assert token not in text, token
