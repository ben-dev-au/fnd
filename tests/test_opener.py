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
    assert opener.explain_open(kind="pdf", page=7, pdf_strategy="applescript").startswith(
        "open <file>"
    )


def test_explain_open_pdf_with_skim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opener, "_has_skim", lambda: True)
    msg = opener.explain_open(kind="pdf", page=7, pdf_strategy="applescript")
    assert "Skim" in msg
    assert "page 7" in msg


def test_explain_open_non_pdf() -> None:
    msg = opener.explain_open(kind="docx", page=0, pdf_strategy="applescript")
    assert msg == "open <file> (default app)"


def urlsafe_unquote(s: str) -> str:
    import urllib.parse

    return urllib.parse.unquote(s)
