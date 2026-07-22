"""Cross-platform PDF viewers + best-effort page-jump promotion.

The registry is OS-agnostic: every viewer self-gates via ``available()`` (a
``which`` / install-path probe), so on any given OS only the viewers actually
installed are offered. These tests drive the argv construction, the probes,
and the Linux/Windows auto-promotion by injecting ``which`` / ``sys.platform``,
so they run identically on every platform in the CI matrix.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fnd import apps, opener
from fnd.apps import OpenRequest


def _capture_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        apps.subprocess,
        "run",
        lambda argv, **kw: captured.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )
    return captured


# ── Handler argv construction ────────────────────────────────────────────


def test_zathura_handler_page_jump_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_argv(monkeypatch)
    apps.BUILTIN_APPS["zathura"].handler(OpenRequest(path=Path("/d/a.pdf"), kind="pdf", page=3))
    assert captured == [["zathura", "--page", "3", "/d/a.pdf"]]


def test_zathura_handler_no_page_opens_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_argv(monkeypatch)
    apps.BUILTIN_APPS["zathura"].handler(OpenRequest(path=Path("/d/a.pdf"), kind="pdf", page=0))
    assert captured == [["zathura", "/d/a.pdf"]]


def test_okular_handler_page_jump_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_argv(monkeypatch)
    apps.BUILTIN_APPS["okular"].handler(OpenRequest(path=Path("/d/a.pdf"), kind="pdf", page=12))
    assert captured == [["okular", "--page", "12", "/d/a.pdf"]]


def test_sumatra_handler_uses_resolved_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps, "_sumatra_exe", lambda: r"C:\Tools\SumatraPDF.exe")
    captured = _capture_argv(monkeypatch)
    apps.BUILTIN_APPS["sumatra"].handler(OpenRequest(path=Path("/d/a.pdf"), kind="pdf", page=5))
    assert captured == [[r"C:\Tools\SumatraPDF.exe", "-page", "5", "/d/a.pdf"]]


# ── Availability probes ──────────────────────────────────────────────────


def test_zathura_available_via_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        apps.shutil, "which", lambda b: "/usr/bin/zathura" if b == "zathura" else None
    )
    assert apps.BUILTIN_APPS["zathura"].available() is True
    monkeypatch.setattr(apps.shutil, "which", lambda _b: None)
    assert apps.BUILTIN_APPS["zathura"].available() is False


def test_sumatra_exe_prefers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        apps.shutil, "which", lambda b: "S:/bin/SumatraPDF.exe" if "Sumatra" in b else None
    )
    assert apps._sumatra_exe() == "S:/bin/SumatraPDF.exe"


def test_sumatra_exe_falls_back_to_install_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(apps.shutil, "which", lambda _b: None)
    exe = tmp_path / "SumatraPDF" / "SumatraPDF.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert apps._sumatra_exe() == str(exe)


def test_sumatra_exe_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps.shutil, "which", lambda _b: None)
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.delenv(env, raising=False)
    assert apps._sumatra_exe() is None


def test_obsidian_available_linux_via_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        apps.shutil, "which", lambda b: "/usr/bin/obsidian" if b == "obsidian" else None
    )
    assert apps._obsidian_app_exists() is True


def test_obsidian_available_windows_via_localappdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    exe = tmp_path / "Obsidian" / "Obsidian.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert apps._obsidian_app_exists() is True


# ── Best-effort auto-promotion on Linux/Windows ──────────────────────────


def test_open_smart_promotes_zathura_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No user pdf default on Linux → the first available page-jump viewer
    (zathura) is promoted and page-jumps."""
    from fnd.config import Config

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("fnd.config.load", lambda *a, **kw: Config())
    monkeypatch.setattr(
        apps.shutil, "which", lambda b: "/usr/bin/zathura" if b == "zathura" else None
    )
    captured = _capture_argv(monkeypatch)

    f = tmp_path / "paper.pdf"
    f.touch()
    opener.open_smart(path=f, kind="pdf", page=7)
    assert captured == [["zathura", "--page", "7", str(f)]]


def test_open_smart_falls_through_to_system_when_no_viewer_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No viewer installed on Linux → system default (launcher.open_path)."""
    from fnd.config import Config

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("fnd.config.load", lambda *a, **kw: Config())
    monkeypatch.setattr(apps.shutil, "which", lambda _b: None)
    opened: list[Path] = []
    monkeypatch.setattr("fnd.launcher.open_path", lambda p: opened.append(Path(p)) or 0)

    f = tmp_path / "paper.pdf"
    f.touch()
    opener.open_smart(path=f, kind="pdf", page=7)
    assert opened == [f]
