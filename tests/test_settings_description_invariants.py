"""Invariants on every selectable MenuItem.

Run as a sweep across every provider so a future drift catches the
test instead of a user finding it:

1. Every selectable row has a non-empty user-facing description.
2. The `↗` glyph never appears in descriptions — it's reserved for
   labels on rows whose ``external_app=True``.
3. Labels don't end with `…` — that affordance moved to the trailing
   `[ Run… ]` button form.
"""

from __future__ import annotations

from typing import cast

import pytest

from fnd.tui.menu import (
    KIND_EXTERNAL,
    KIND_HEADER,
    MenuItem,
    build_root_items,
    section_items,
)

# Provider IDs we can walk without a real config object.
_SECTION_IDS = ("preferences", "collections", "keybindings", "indexing-pdf-texture")


class _StubApp:
    def __init__(self) -> None:
        self._config = None
        self._fnd_keymap = type("KM", (), {"bindings": {}})()
        self._collections: list[str] = []
        self._highlights_enabled = True


def _all_selectable() -> list[MenuItem]:
    from fnd.tui.app import FNDApp

    app = cast(FNDApp, _StubApp())
    items: list[MenuItem] = list(build_root_items(app))
    for sid in _SECTION_IDS:
        items.extend(section_items(app, sid))
    return [it for it in items if it.kind != KIND_HEADER]


@pytest.mark.asyncio
async def test_rows_needing_a_real_config_also_carry_descriptions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The sweep above walks providers with no config, so the screens that
    need one — a collection, its sources, the source form — were never
    reached, and rows there shipped bare."""
    from fnd.config import CollectionConfig, SourceConfig, write_collection
    from fnd.tui import FNDApp
    from fnd.tui.menu import _provider_collection, _provider_sources
    from fnd.tui.settings_screen import SettingsList, SourceFormScreen

    cfg_path = tmp_path / "config.toml"
    root = tmp_path / "vault"
    root.mkdir()
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(sources=[SourceConfig(path=root)]),
    )
    from fnd.config import load

    app = FNDApp(index_dir=None, config=load(cfg_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        rows: list[MenuItem] = [
            *_provider_collection(app, "probe"),
            *_provider_sources(app, "probe"),
        ]
        app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
        for _ in range(30):
            await pilot.pause()
        rows.extend(app.screen.query_one(SettingsList)._items)

    bare = [it.id for it in rows if it.kind != KIND_HEADER and len(it.description or "") < 9]
    assert not bare, f"rows with no usable description: {bare}"


def test_every_row_has_a_user_facing_description() -> None:
    """Sweep: each selectable row has a description ≥ 15 chars.

    A few short labels ("Quit fnd.") are acceptable when the entire
    behaviour fits in a sentence; allow ≥ 9 chars for those."""
    items = _all_selectable()
    too_short = [(it.id, it.description) for it in items if len(it.description or "") < 9]
    assert not too_short, f"rows with descriptions <9 chars: {too_short}"


def test_no_descriptions_contain_external_arrow() -> None:
    """↗ is a visual affordance — never in description prose.

    The render layer adds the leading ↗ on rows where
    ``external_app=True``; if anything ships ↗ in description text,
    the row is double-signalling and the glyph reads as decoration
    instead of affordance."""
    items = _all_selectable()
    offenders = [(it.id, it.description) for it in items if "↗" in (it.description or "")]
    assert not offenders, f"rows with ↗ in description: {offenders}"


def test_external_arrow_only_appears_on_external_app_labels() -> None:
    """↗ on a label is allowed only when ``external_app=True``."""
    items = _all_selectable()
    for it in items:
        if "↗" in it.label:
            assert it.kind == KIND_EXTERNAL, f"row {it.id} has ↗ in label but kind != KIND_EXTERNAL"
            assert it.external_app, f"row {it.id} has ↗ in label but external_app=False"


def test_no_labels_end_with_ellipsis() -> None:
    """Labels in normal settings rows don't end with `…` — that
    affordance lives in the trailing ``[ Run… ]`` action-button form
    (i.e. on ``action_label``, not the row label).

    Keybindings cheat-sheet rows (``key.*``) are exempt: they mirror
    the action's own display name, which fnd's action registry uses
    `…` on naturally (e.g. ``Open with…`` for the picker)."""
    items = _all_selectable()
    offenders = [
        (it.id, it.label)
        for it in items
        if it.label.rstrip().endswith("…") and not it.id.startswith("key.")
    ]
    assert not offenders, f"rows with label ending in …: {offenders}"


@pytest.mark.parametrize("section_id", _SECTION_IDS)
def test_each_section_yields_rows(section_id: str) -> None:
    """Every named section returns at least one row (sanity)."""
    from fnd.tui.app import FNDApp

    app = cast(FNDApp, _StubApp())
    items = section_items(app, section_id)
    assert items, f"section {section_id!r} returned no rows"


def test_action_labels_are_not_generic_open() -> None:
    """`[ Open ]` was a stopgap. The button verb must mirror what the
    action does so it reads naturally next to the row label. An
    "Uninstall pdf-structure" row with a trailing `[ Open ]` would
    confuse the user about what Enter actually does.

    Allowed verb-only exceptions: the "Add" verb on collections.add
    is unambiguous because the label "Add collection" matches it.
    """
    from fnd.tui.menu import KIND_ACTION

    items = _all_selectable()
    offenders = []
    for it in items:
        if it.kind != KIND_ACTION:
            continue
        # The default action verb is "Run" — acceptable as a generic
        # fallback. "Open" is too vague; reject it.
        if it.action_label.strip().rstrip("…") == "Open":
            offenders.append((it.id, it.label, it.action_label))
    assert not offenders, (
        f"action rows with generic '[ Open ]' verb: {offenders}. "
        "Use a verb that mirrors the action (Install / Uninstall / "
        "Update / Prune / Delete / Clear)."
    )
