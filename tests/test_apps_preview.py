"""Open-PDF-in-Preview targets the correct document + page.

Guards the regression where the page-jump matched the front document by
substring and sent "go to page" keystrokes to whatever was frontmost — so
opening a PDF behind an already-open one paged the wrong document.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd import apps


def test_preview_script_uses_exact_path_match() -> None:
    s = apps._PREVIEW_PAGE_JUMP_SCRIPT
    assert "contains pdfPath" not in s  # the substring-match bug
    assert "(path of d) is pdfPath" in s  # exact match


def test_preview_script_gates_keystrokes_on_front_document() -> None:
    """`matched` must be set from confirming the front document IS our target
    (defends against window-name collisions + open/window timing), and the
    keystrokes must be gated by it — never page a doc we can't confirm front."""
    s = apps._PREVIEW_PAGE_JUMP_SCRIPT
    assert "path of front document" in s
    # The front-document confirmation drives `matched`, which gates keystrokes.
    assert s.index("path of front document") < s.index('keystroke "g"')
    assert s.index("if matched then") < s.index('keystroke "g"')


def test_handle_preview_passes_resolved_path_and_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler launches the page-jump with the hit's path canonicalised
    (Preview reports resolved paths, so the script's exact match needs the
    realpath) and the target page as separate argv items."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], check: bool = False) -> object:
        captured["cmd"] = cmd

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(apps.subprocess, "run", fake_run)
    monkeypatch.setattr(apps, "ax_trusted", lambda: True)
    raw = Path("/tmp/a report.pdf")
    apps._handle_preview(apps.OpenRequest(path=raw, kind="pdf", page=5))
    assert str(raw.resolve()) in captured["cmd"]  # canonical path, spaces preserved
    assert "5" in captured["cmd"]


def test_handle_preview_falls_back_without_ax(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Accessibility → open on page 1 via `open -a Preview`, no osascript."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], check: bool = False) -> object:
        captured["cmd"] = cmd

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(apps.subprocess, "run", fake_run)
    monkeypatch.setattr(apps, "ax_trusted", lambda: False)
    monkeypatch.setattr(apps, "_emit_notice", lambda _msg: None)
    req = apps.OpenRequest(path=Path("/tmp/doc.pdf"), kind="pdf", page=5)
    apps._handle_preview(req)
    assert captured["cmd"][:3] == ["open", "-a", "Preview"]
    assert "osascript" not in captured["cmd"]
