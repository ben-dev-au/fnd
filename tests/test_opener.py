"""Phase 5: opener URL formatting + dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd import opener


def test_skim_url_simple(tmp_path: Path) -> None:
    f = tmp_path / "paper.pdf"
    f.touch()
    url = opener.skim_url(f, 7)
    assert url.startswith("skim:///")
    assert url.endswith("#page=7")
    assert str(f.resolve()) in urlsafe_unquote(url)


def test_skim_url_spaces_are_encoded(tmp_path: Path) -> None:
    f = tmp_path / "paper with spaces.pdf"
    f.touch()
    url = opener.skim_url(f, 14)
    assert "%20" in url
    assert "#page=14" in url


def test_skim_url_ampersand_encoded(tmp_path: Path) -> None:
    f = tmp_path / "a & b.pdf"
    f.touch()
    url = opener.skim_url(f, 1)
    assert "%26" in url, url


def test_skim_url_unicode(tmp_path: Path) -> None:
    f = tmp_path / "résumé.pdf"
    f.touch()
    url = opener.skim_url(f, 1)
    # Unicode percent-encoded as UTF-8.
    assert "%C3%A9" in url, url


def test_explain_open_falls_back_when_no_skim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opener, "_has_skim", lambda: False)
    assert opener.explain_open(kind="pdf", page=7, pdf_strategy="url").startswith("open <file>")


def test_explain_open_pdf_with_skim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opener, "_has_skim", lambda: True)
    msg = opener.explain_open(kind="pdf", page=7, pdf_strategy="url")
    assert "skim://" in msg
    assert "page=7" in msg


def test_explain_open_non_pdf() -> None:
    msg = opener.explain_open(kind="docx", page=0, pdf_strategy="url")
    assert msg == "open <file> (default app)"


def urlsafe_unquote(s: str) -> str:
    import urllib.parse

    return urllib.parse.unquote(s)


# ── Phase 1d: conditional PDF auto-promote ──────────────────────────────


def test_open_smart_auto_promotes_preview_when_no_skim_and_ax_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Skim, Preview installed, AX granted → page-jump via Preview."""
    from fnd import apps as apps_mod

    apps_mod._reset_ax_cache()
    monkeypatch.setattr(opener, "_has_skim", lambda: False)
    monkeypatch.setattr(apps_mod, "_preview_app_exists", lambda: True)
    monkeypatch.setattr(apps_mod, "_probe_ax_trusted", lambda: True)
    # Make config loading return a fresh empty Config so no user override.
    from fnd.config import Config

    monkeypatch.setattr("fnd.config.load", lambda *a, **kw: Config())

    captured: list[list[str]] = []
    monkeypatch.setattr(
        apps_mod.subprocess,
        "run",
        lambda argv, **kw: captured.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )

    f = tmp_path / "paper.pdf"
    f.touch()
    opener.open_smart(path=f, kind="pdf", page=5)
    assert captured, "expected at least one subprocess.run call"
    argv = captured[0]
    assert argv[0] == "osascript", f"expected osascript dispatch, got {argv}"
    assert str(f) in argv
    assert "5" in argv


def test_open_smart_falls_through_to_system_when_no_skim_no_ax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Skim, no AX → ``open <pdf>`` (system default, no page jump)."""
    from fnd import apps as apps_mod

    apps_mod._reset_ax_cache()
    monkeypatch.setattr(opener, "_has_skim", lambda: False)
    monkeypatch.setattr(apps_mod, "_preview_app_exists", lambda: True)
    monkeypatch.setattr(apps_mod, "_probe_ax_trusted", lambda: False)
    from fnd.config import Config

    monkeypatch.setattr("fnd.config.load", lambda *a, **kw: Config())

    captured: list[list[str]] = []
    monkeypatch.setattr(
        apps_mod.subprocess,
        "run",
        lambda argv, **kw: captured.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )

    f = tmp_path / "paper.pdf"
    f.touch()
    opener.open_smart(path=f, kind="pdf", page=5)
    # System fallback uses plain ``open <path>``.
    assert captured == [["open", str(f)]], f"got {captured}"


def test_open_smart_user_override_wins_over_auto_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``app_defaults.pdf = "system"`` must beat the Skim
    auto-promote even when Skim is installed."""
    from fnd import apps as apps_mod
    from fnd.config import Config

    apps_mod._reset_ax_cache()
    monkeypatch.setattr(opener, "_has_skim", lambda: True)
    monkeypatch.setattr(
        "fnd.config.load",
        lambda *a, **kw: Config.model_validate({"app_defaults": {"pdf": "system"}}),
    )

    captured: list[list[str]] = []
    monkeypatch.setattr(
        apps_mod.subprocess,
        "run",
        lambda argv, **kw: captured.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )

    f = tmp_path / "paper.pdf"
    f.touch()
    opener.open_smart(path=f, kind="pdf", page=5)
    assert captured == [["open", str(f)]], f"user override ignored: {captured}"
