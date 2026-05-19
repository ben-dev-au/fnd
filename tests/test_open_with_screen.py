"""Phase 3: Shift-O 'Open with…' modal picker.

Contract:

* Lists every registered app whose ``handles`` covers ``hit.kind`` (plus
  the wildcard-``*`` apps), filtered to ``available()`` ones.
* Resolved default is highlighted; pressing Enter fires it.
* Letter-shortcut per row (first unique letter of the display name);
  pressing it dispatches directly without Enter.
* Esc dismisses without firing anything.
* Successful dispatch returns the registry id via ``ModalScreen.dismiss``
  so the caller can update status / notify on exit code.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.app import App

from fnd import apps
from fnd.tui.open_with_screen import OpenWithScreen, eligible_apps, letter_shortcuts

# ── Eligibility + shortcut assignment (pure functions, no Pilot) ────────


def test_eligible_apps_for_pdf_lists_pdf_capable() -> None:
    """For a PDF hit, every PDF-capable + available app appears, plus
    system (which has the wildcard ``*`` handle)."""
    rows = eligible_apps(
        kind="pdf",
        registry=apps.BUILTIN_APPS,
        availability={"system": True, "skim": True, "preview": False, "vscode": False},
    )
    ids = [r.id for r in rows]
    assert "skim" in ids
    assert "system" in ids
    assert "preview" not in ids  # marked unavailable above


def test_eligible_apps_for_md_lists_md_capable() -> None:
    rows = eligible_apps(
        kind="md",
        registry=apps.BUILTIN_APPS,
        availability={
            "system": True,
            "obsidian": True,
            "vscode": True,
            "skim": True,  # not eligible — handles only pdf
        },
    )
    ids = [r.id for r in rows]
    assert "obsidian" in ids
    assert "vscode" in ids
    assert "system" in ids
    assert "skim" not in ids


def test_eligible_apps_default_first_when_present() -> None:
    """The resolved default sorts to the top of the menu."""
    rows = eligible_apps(
        kind="pdf",
        registry=apps.BUILTIN_APPS,
        availability={"system": True, "skim": True, "preview": True},
        default_id="preview",
    )
    assert rows[0].id == "preview"


def test_letter_shortcuts_unique_first_letters() -> None:
    rows = [
        SimpleNamespace(id="skim", display_name="Skim"),
        SimpleNamespace(id="preview", display_name="Preview"),
        SimpleNamespace(id="system", display_name="System Default"),
    ]
    keys = letter_shortcuts(rows)
    assert keys["skim"] == "s"
    assert keys["preview"] == "p"
    # System Default — 's' taken by skim, walk to next letter.
    assert keys["system"] != "s"
    assert keys["system"] != "p"


def test_letter_shortcuts_collide_falls_back_to_index() -> None:
    """When even fallback letters collide, use index digits."""
    rows = [SimpleNamespace(id=f"app_{i}", display_name=f"App{i}") for i in range(10)]
    keys = letter_shortcuts(rows)
    # All 10 entries get a key, even if many start with 'a' / 'A'.
    assert len(keys) == 10
    assert len(set(keys.values())) == 10


# ── Modal Pilot tests ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_ax(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps._reset_ax_cache()
    monkeypatch.setattr(apps, "_probe_ax_trusted", lambda: True)
    apps.set_notice_sink(None)


def _fake_run(captured: list[list[str]]) -> Any:
    return lambda argv, **kw: (captured.append(list(argv)) or type("R", (), {"returncode": 0})())


@pytest.mark.asyncio
async def test_modal_shows_pdf_apps_and_enter_fires_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(apps, "_skim_app_exists", lambda: True)
    monkeypatch.setattr(apps, "_preview_app_exists", lambda: True)
    captured: list[list[str]] = []
    monkeypatch.setattr(apps.subprocess, "run", _fake_run(captured))

    pdf = tmp_path / "doc.pdf"
    pdf.touch()

    hit = SimpleNamespace(path=str(pdf), kind="pdf", page=7, heading_path="")

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(
                OpenWithScreen(
                    hit=hit,
                    source=None,
                    registry=apps.BUILTIN_APPS,
                    default_id="skim",  # explicit; modal honours it
                )
            )

    async with Host().run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    # Default was skim → opens skim:// URL via `open <url>`.
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "open"
    assert argv[1].startswith("skim://")
    assert "page=7" in argv[1]


@pytest.mark.asyncio
async def test_modal_letter_shortcut_fires_specific_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(apps, "_skim_app_exists", lambda: True)
    monkeypatch.setattr(apps, "_preview_app_exists", lambda: True)
    captured: list[list[str]] = []
    monkeypatch.setattr(apps.subprocess, "run", _fake_run(captured))

    pdf = tmp_path / "doc.pdf"
    pdf.touch()
    hit = SimpleNamespace(path=str(pdf), kind="pdf", page=2, heading_path="")

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(
                OpenWithScreen(
                    hit=hit,
                    source=None,
                    registry=apps.BUILTIN_APPS,
                    default_id="skim",
                )
            )

    async with Host().run_test() as pilot:
        await pilot.pause()
        # Press 'p' to pick Preview directly (skipping the highlighted skim).
        await pilot.press("p")
        await pilot.pause()

    assert len(captured) == 1
    argv = captured[0]
    # Preview handler with AX granted → osascript dispatch.
    assert argv[0] == "osascript"


@pytest.mark.asyncio
async def test_modal_escape_dismisses_without_firing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(apps, "_skim_app_exists", lambda: True)
    captured: list[list[str]] = []
    monkeypatch.setattr(apps.subprocess, "run", _fake_run(captured))

    pdf = tmp_path / "doc.pdf"
    pdf.touch()
    hit = SimpleNamespace(path=str(pdf), kind="pdf", page=1, heading_path="")

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(
                OpenWithScreen(
                    hit=hit,
                    source=None,
                    registry=apps.BUILTIN_APPS,
                    default_id="skim",
                )
            )

    async with Host().run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert captured == [], "no app should have been launched on dismiss"


@pytest.mark.asyncio
async def test_modal_md_hit_lists_obsidian_when_vault_known(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MD hit + obsidian-tagged source → Obsidian appears, gets the
    vault from app_params, and dispatches via obsidian:// URL."""
    monkeypatch.setattr(apps, "_obsidian_app_exists", lambda: True)
    captured: list[list[str]] = []
    monkeypatch.setattr(apps.subprocess, "run", _fake_run(captured))

    md = tmp_path / "vault" / "note.md"
    md.parent.mkdir()
    md.touch()
    hit = SimpleNamespace(path=str(md), kind="md", page=0, heading_path="Hi")
    source = SimpleNamespace(
        path=md.parent,
        app="obsidian",
        app_for={},
        app_params={"vault": "MyVault"},
    )

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(
                OpenWithScreen(
                    hit=hit,
                    source=source,
                    registry=apps.BUILTIN_APPS,
                    default_id="obsidian",
                )
            )

    async with Host().run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "open"
    assert argv[1].startswith("obsidian://open")
    assert "vault=MyVault" in argv[1]
    assert "%23Hi" in argv[1]
