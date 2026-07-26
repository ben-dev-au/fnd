"""Reveal-in-file-manager: the action registry entry, the ``FNDApp`` handler,
and the Open-with picker row.

``fnd.opener.reveal`` / ``fnd.launcher.reveal`` already do the per-OS spawn
(covered by ``tests/test_launcher.py``); these tests cover the surfaces that
reach them, so the reveal is actually reachable from the results pane rather
than existing only as a helper.

Captured at the launcher seam, so they assert identically on every OS in the
CI matrix.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.app import App
from textual.widgets import Tree

from fnd import apps, launcher, os_labels
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.actions import REGISTRY, load_keymap
from fnd.tui.open_with_screen import OpenWithScreen, eligible_apps

ACTION_ID = "reveal_in_file_manager"


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def revealed(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Capture every reveal at the one seam all callers funnel through."""
    seen: list[Path] = []
    monkeypatch.setattr(launcher, "reveal", lambda p: seen.append(Path(p)))
    return seen


# ── Action registry ──────────────────────────────────────────────────────


def test_reveal_action_is_registered() -> None:
    action = next((a for a in REGISTRY if a.id == ACTION_ID), None)
    assert action is not None, [a.id for a in REGISTRY]
    assert action.palette_command == "reveal"


def test_reveal_action_has_a_default_key() -> None:
    """Shift+R — `R` pairs with `r` (focus results) the way `O` pairs with `o`."""
    action = next(a for a in REGISTRY if a.id == ACTION_ID)
    assert action.default_key == "R"
    assert load_keymap().bindings["R"] == ACTION_ID


def test_reveal_action_is_offered_where_a_result_is_focused() -> None:
    """Both panes track the same results cursor, so both can reveal it."""
    action = next(a for a in REGISTRY if a.id == ACTION_ID)
    assert action.contexts == ("results", "preview")


def test_reveal_action_label_defers_the_file_manager_name_to_render_time() -> None:
    """The registry is a module constant, so it carries the placeholder rather
    than a baked-in OS name — see ``tests/test_keybindings_os_vocabulary.py``
    for the per-platform rendering."""
    action = next(a for a in REGISTRY if a.id == ACTION_ID)
    footer_label = action.footer_label
    assert footer_label is not None
    assert footer_label == os_labels.REVEAL_LABEL
    assert os_labels.FILE_MANAGER in action.description
    assert os_labels.localise(footer_label) == os_labels.reveal_label()


def test_reveal_key_does_not_collide_with_another_action() -> None:
    keys = [k for a in REGISTRY if a.default_key for k in a.default_key.split(",")]
    assert keys.count("R") == 1, keys


# ── FNDApp handler ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_action_reveals_the_focused_result(
    built_index: Path, revealed: list[Path]
) -> None:
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        app.action_reveal_in_file_manager()
        await pilot.pause()

    assert revealed, "no reveal reached the launcher seam"
    assert revealed[-1].suffix, revealed[-1]


@pytest.mark.asyncio
async def test_reveal_key_press_reaches_the_action(built_index: Path, revealed: list[Path]) -> None:
    """Bound via the registry, so the key must work without a manual call."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.press("R")
        await pilot.pause()

    assert revealed, "pressing R did not reveal anything"


@pytest.mark.asyncio
async def test_reveal_is_a_no_op_with_no_result_focused(
    built_index: Path, revealed: list[Path]
) -> None:
    """A query with no hits leaves the cursor on nothing — reveal must not
    raise or spawn a file manager on a bogus path."""
    app = FNDApp(index_dir=built_index, initial_query="zzzznosuchtermzzzz")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_reveal_in_file_manager()
        await pilot.pause()

    assert revealed == []


# ── Open-with picker row ─────────────────────────────────────────────────


def test_reveal_app_is_registered_for_every_file_type() -> None:
    app = apps.BUILTIN_APPS.get("reveal")
    assert app is not None, sorted(apps.BUILTIN_APPS)
    assert app.handles == ("*",)
    assert app.available() is True  # every OS has a file manager
    assert app.display_name == os_labels.reveal_label()


@pytest.mark.parametrize("kind", ["pdf", "md", "docx", "python", "epub"])
def test_reveal_row_appears_in_the_picker_for_any_kind(kind: str) -> None:
    ids = [r.id for r in eligible_apps(kind=kind, registry=apps.BUILTIN_APPS)]
    assert "reveal" in ids, ids


def test_reveal_sorts_last_so_it_never_pushes_openers_down() -> None:
    """It's an escape hatch, not an opener — the apps that actually open the
    file stay at the top of the list."""
    ids = [r.id for r in eligible_apps(kind="pdf", registry=apps.BUILTIN_APPS)]
    assert ids[-1] == "reveal", ids


def test_reveal_handler_reveals_rather_than_opens(
    tmp_path: Path, revealed: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(launcher, "open_path", lambda p: opened.append(Path(p)) or 0)
    doc = tmp_path / "notes.md"
    doc.touch()

    rc = apps.BUILTIN_APPS["reveal"].handler(apps.OpenRequest(path=doc, kind="md"))

    assert rc == 0
    assert revealed == [doc]
    assert opened == [], "reveal must not also open the file"


def test_reveal_is_not_offered_as_a_default_app() -> None:
    """Picking it as the default for a kind would make `o` stop opening files,
    so it's excluded from the default-app pickers while staying in Open-with."""
    assert apps.BUILTIN_APPS["reveal"].selectable_default is False
    assert apps.BUILTIN_APPS["system"].selectable_default is True


def test_reveal_never_becomes_the_resolved_default() -> None:
    """`o` must keep opening even though a wildcard-handling app was added."""
    resolved = apps.resolve_app(
        kind="pdf", source=None, app_defaults={}, registry=apps.BUILTIN_APPS
    )
    assert resolved.id == "system"


@pytest.mark.asyncio
async def test_picker_row_fires_the_reveal(tmp_path: Path, revealed: list[Path]) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.touch()
    hit = SimpleNamespace(path=str(pdf), kind="pdf", page=4, heading_path="")

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(
                OpenWithScreen(
                    hit=hit,
                    source=None,
                    registry=apps.BUILTIN_APPS,
                    default_id="system",
                )
            )

    async with Host().run_test() as pilot:
        await pilot.pause()
        rows: Any = pilot.app.screen._rows  # type: ignore[attr-defined]
        shortcuts: Any = pilot.app.screen._shortcuts  # type: ignore[attr-defined]
        assert any(r.id == "reveal" for r in rows), [r.id for r in rows]
        await pilot.press(shortcuts["reveal"])
        await pilot.pause()

    assert revealed == [pdf]
