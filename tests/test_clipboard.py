"""System-clipboard seam: platform resolution + injectable runner."""

from __future__ import annotations

import pytest

from fnd.tui import clipboard


def test_argv_per_platform() -> None:
    assert clipboard.clipboard_argv("Darwin") == ["pbcopy"]
    assert clipboard.clipboard_argv("Windows") == ["clip"]


def test_argv_unknown_platform_with_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the POSIX branch to find nothing installed.
    monkeypatch.setattr("fnd.tui.clipboard.which", lambda _name: None)
    assert clipboard.clipboard_argv("Linux") is None


def test_argv_posix_prefers_installed_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fnd.tui.clipboard.which", lambda name: name == "xclip")
    assert clipboard.clipboard_argv("Linux") == ["xclip", "-selection", "clipboard"]


def test_copy_text_runs_resolved_backend_without_spawning() -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], data: bytes) -> int:
        seen["argv"] = argv
        seen["data"] = data
        return 0

    clipboard.copy_text("hello", argv_for=lambda _s: ["pbcopy"], run=fake_run)
    assert seen == {"argv": ["pbcopy"], "data": b"hello"}


def test_copy_text_raises_when_no_backend() -> None:
    with pytest.raises(OSError, match="no clipboard backend"):
        clipboard.copy_text("x", argv_for=lambda _s: None)


def test_copy_text_raises_on_nonzero_exit() -> None:
    with pytest.raises(OSError, match="exited with"):
        clipboard.copy_text("x", argv_for=lambda _s: ["pbcopy"], run=lambda _a, _d: 1)
