"""Phase 2c: Keybindings cheat-sheet derives from the live action
registry and reorders per the calling-screen context hint.

The old _provider_keybindings used hand-curated tables that drifted
out of date as the Action registry grew. The new provider walks
``fnd.tui.actions.REGISTRY`` so every action with a default key
surfaces automatically.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from fnd.tui import FNDApp
from fnd.tui.actions import REGISTRY
from fnd.tui.menu import MenuItem, _provider_keybindings


def _fake_app() -> FNDApp:
    """Provider ignores its ``_app`` arg; cast keeps pyright happy."""
    return cast(FNDApp, SimpleNamespace())


def _headers(items: tuple[MenuItem, ...]) -> list[str]:
    return [it.label for it in items if it.is_header]


def _rows_under(items: tuple[MenuItem, ...], section_label: str) -> list[tuple[str, str]]:
    """Return (key, label) for every row under the given section."""
    out: list[tuple[str, str]] = []
    in_section = False
    for it in items:
        if it.is_header:
            in_section = it.label == section_label
            continue
        if in_section:
            out.append((it.key, it.label))
    return out


def test_every_action_with_default_key_appears() -> None:
    """No more drift: an action added to the registry with a
    ``default_key`` automatically shows up in the cheat sheet."""
    items = _provider_keybindings(_fake_app())
    seen_action_ids = {it.action_id for it in items if not it.is_header}
    expected = {a.id for a in REGISTRY if a.default_key is not None}
    missing = expected - seen_action_ids
    assert not missing, f"actions missing from Keybindings: {sorted(missing)}"


def test_pretty_key_substitutions_are_applied() -> None:
    items = _provider_keybindings(_fake_app())
    keys = [it.key for it in items if not it.is_header]
    assert "/" in keys, keys  # 'slash' → '/'
    assert "?" in keys, keys  # 'question_mark' → '?'
    assert ":" in keys, keys  # 'colon' → ':'
    assert "Ctrl+F" in keys, keys  # 'ctrl+f' → 'Ctrl+F'
    assert "Space" in keys, keys  # 'space' → 'Space'


def test_static_sections_present() -> None:
    """Widget bindings outside the action registry (Settings menu,
    source form, Open-with modal, AX modal) are listed too."""
    items = _provider_keybindings(_fake_app())
    headers = _headers(items)
    for needed in (
        "Global",
        "Settings menu",
        "Source form",
        "Open with… modal",
        "Accessibility prompt",
    ):
        assert needed in headers, f"missing section: {needed}"


def test_source_form_section_lists_ctrl_d_and_ctrl_s() -> None:
    """Phase 5 added Ctrl+D source delete — must be in the cheat sheet."""
    items = _provider_keybindings(_fake_app())
    rows = _rows_under(items, "Source form")
    keys = [k for k, _ in rows]
    assert any("Ctrl+S" in k for k in keys), keys
    assert any("Ctrl+D" in k for k in keys), keys


def test_context_hint_lifts_named_section_to_just_after_global() -> None:
    items = _provider_keybindings(_fake_app(), context_hint="Source form")
    headers = _headers(items)
    assert headers[0] == "Global"
    assert headers[1] == "Source form", headers


def test_context_hint_open_with_modal_is_recognised() -> None:
    items = _provider_keybindings(_fake_app(), context_hint="Open with… modal")
    headers = _headers(items)
    assert headers[1] == "Open with… modal", headers


def test_context_hint_none_keeps_default_order() -> None:
    items = _provider_keybindings(_fake_app())
    headers = _headers(items)
    assert headers[0] == "Global"
    # When no hint is supplied, the order should follow the bucket
    # declaration order — Results pane comes before Preview pane.
    if "Results pane" in headers and "Preview pane" in headers:
        assert headers.index("Results pane") < headers.index("Preview pane")


def test_unknown_context_hint_falls_back_to_default_order() -> None:
    items = _provider_keybindings(_fake_app(), context_hint="Bogus section")
    headers = _headers(items)
    assert headers[0] == "Global"  # didn't crash, sane default


def test_context_hint_marks_matching_header_for_highlight() -> None:
    """The hint section's header carries the ``_hint_section_`` sentinel
    in its ``keywords`` — SettingsList reads that to apply the
    ``-hint-section`` CSS class so the band paints with the accent
    border + bg. Without this marker the section just sits at the top
    of the list with no visual cue."""
    items = _provider_keybindings(_fake_app(), context_hint="Source form")
    hint_headers = [it for it in items if it.is_header and "_hint_section_" in (it.keywords or ())]
    assert len(hint_headers) == 1, [h.label for h in hint_headers]
    assert hint_headers[0].label == "Source form"


def test_no_context_hint_means_no_highlighted_header() -> None:
    items = _provider_keybindings(_fake_app())
    hint_headers = [it for it in items if it.is_header and "_hint_section_" in (it.keywords or ())]
    assert hint_headers == []


def test_every_key_row_label_differs_from_description() -> None:
    """The DetailStrip surfaces ``description`` when a row is focused —
    if label == description, the strip just echoes the row label and
    adds zero value. Every row must carry a short label AND a
    distinct long-form description."""
    items = _provider_keybindings(_fake_app())
    duplicates = [it.label for it in items if not it.is_header and it.label == it.description]
    assert not duplicates, (
        f"{len(duplicates)} keybindings rows duplicate label==description; "
        "DetailStrip would just echo the row label. Set a short label "
        "and keep description for the long-form. Examples: "
        f"{duplicates[:5]}"
    )


def test_every_key_row_has_a_description() -> None:
    """A row with no description leaves the DetailStrip empty when
    focused — every keybinding should have a long-form explanation."""
    items = _provider_keybindings(_fake_app())
    missing = [it.label for it in items if not it.is_header and not it.description]
    assert not missing, missing
