"""Phase 1c: in-app modal explaining macOS Accessibility denial.

The flow under test:

1. ``fnd.apps.set_notice_sink`` installed by ``FNDApp.on_mount`` routes
   AX-related ``_emit_notice`` calls to ``AccessibilityPermissionScreen``.
2. Other notices fall through to ``self.notify`` (toast).
3. Pressing 'r' on the modal calls :func:`fnd.apps._reset_ax_cache`, so
   a subsequent open attempt re-probes AX without restarting fnd.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd import apps


def test_set_notice_sink_routes_through_sink() -> None:
    apps._reset_ax_cache()
    seen: list[str] = []
    apps.set_notice_sink(seen.append)
    try:
        apps._emit_notice("hello from test")
        assert seen == ["hello from test"]
    finally:
        apps.set_notice_sink(None)
        apps._reset_ax_cache()


def test_notice_dedup_still_holds_with_sink() -> None:
    apps._reset_ax_cache()
    seen: list[str] = []
    apps.set_notice_sink(seen.append)
    try:
        apps._emit_notice("dup")
        apps._emit_notice("dup")
        assert seen == ["dup"]  # second call dedup'd
    finally:
        apps.set_notice_sink(None)
        apps._reset_ax_cache()


def test_set_notice_sink_none_restores_stderr_fallback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    apps._reset_ax_cache()
    apps.set_notice_sink(None)
    apps._emit_notice("falls to stderr")
    captured = capsys.readouterr()
    assert "falls to stderr" in captured.err


# ── ax_permission_screen smoke test (no app instance needed) ───────────


def test_modal_module_imports_cleanly() -> None:
    """Import alone catches typos in the modal's CSS / binding spec."""
    from fnd.tui import ax_permission_screen

    assert ax_permission_screen.AccessibilityPermissionScreen is not None


@pytest.mark.asyncio
async def test_modal_retry_action_clears_ax_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pressing 'r' on the modal must reset the AX cache so the very
    next open attempt re-probes (instead of returning the stale False)."""
    from textual.app import App

    from fnd.tui.ax_permission_screen import AccessibilityPermissionScreen

    apps._reset_ax_cache()
    monkeypatch.setattr(apps, "_probe_ax_trusted", lambda: False)
    assert apps.ax_trusted() is False
    # Cache is now populated.
    assert "value" in apps._ax_cache

    class Host(App[None]):
        async def on_mount(self) -> None:
            await self.push_screen(AccessibilityPermissionScreen())

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()

    assert "value" not in apps._ax_cache, "retry must clear the AX cache"


def test_modal_open_settings_action_invokes_open_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Open System Settings button must fire ``open <url>`` with the
    Accessibility deep-link URL."""
    from fnd.tui import ax_permission_screen

    captured: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv: list[str], **kw: Any) -> None:
            captured.append(list(argv))

    monkeypatch.setattr(ax_permission_screen.subprocess, "Popen", FakePopen)
    screen = ax_permission_screen.AccessibilityPermissionScreen()
    screen.action_open_settings()
    assert captured == [
        [
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ]
    ]


# ── Preview handler triggers the notice on AX-denied + page-jump ────


def test_preview_handler_emits_ax_notice_when_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AX is denied AND the request has a page locator, the Preview
    handler must (a) call _emit_notice with the AX message, (b) fall
    back to `open -a Preview <path>`. The user sees the file on page 1
    and a modal explaining the issue."""
    apps._reset_ax_cache()
    monkeypatch.setattr(apps, "_probe_ax_trusted", lambda: False)

    notices: list[str] = []
    apps.set_notice_sink(notices.append)

    captured_run: list[list[str]] = []
    monkeypatch.setattr(
        apps.subprocess,
        "run",
        lambda argv, **kw: captured_run.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )

    try:
        req = apps.OpenRequest(path=Path("/tmp/file.pdf"), kind="pdf", page=12)
        rc = apps.BUILTIN_APPS["preview"].handler(req)
        assert rc == 0
        assert captured_run == [["open", "-a", "Preview", str(req.path)]]
        assert len(notices) == 1
        assert "Accessibility" in notices[0]
    finally:
        apps.set_notice_sink(None)
        apps._reset_ax_cache()


def test_preview_handler_silent_when_no_page_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``page == 0`` means "open this PDF" with no position request — no
    page-jump attempt, so no Accessibility-permission notice."""
    apps._reset_ax_cache()
    monkeypatch.setattr(apps, "_probe_ax_trusted", lambda: False)

    notices: list[str] = []
    apps.set_notice_sink(notices.append)
    monkeypatch.setattr(
        apps.subprocess,
        "run",
        lambda argv, **kw: type("R", (), {"returncode": 0})(),
    )
    try:
        req = apps.OpenRequest(path=Path("/tmp/file.pdf"), kind="pdf", page=0)
        apps.BUILTIN_APPS["preview"].handler(req)
        assert notices == []
    finally:
        apps.set_notice_sink(None)
        apps._reset_ax_cache()
