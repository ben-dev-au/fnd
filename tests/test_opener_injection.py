"""Filename-injection regression tests for the Skim open path.

The previous AppleScript opener escaped only ``\\`` and ``"`` — a filename
containing a newline could break out of the AppleScript string literal and
inject arbitrary ``osascript`` commands. The URL form goes through
``urllib.parse.quote`` and reaches ``open`` as an argv element, so every
byte percent-encodes. These tests pin that behaviour.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from fnd import opener


@pytest.mark.parametrize(
    "evil_name",
    [
        'newline\ntell application "Terminal" to do shell script "touch /tmp/pwn".pdf',
        "carriage\rreturn.pdf",
        "tab\tcharacter.pdf",
        'double"quote.pdf',
        "back\\slash.pdf",
        "ampersand&query=stuff.pdf",
        "hash#fragment.pdf",
        "question?mark.pdf",
        "space and ' apostrophe.pdf",
        "résumé_unicode.pdf",
    ],
)
def test_skim_url_neutralises_adversarial_filenames(tmp_path: Path, evil_name: str) -> None:
    # No `.touch()` — we don't need the file to exist on disk. `resolve()`
    # works on absent paths and the security claim is purely about
    # encoding, not filesystem state. (pytest's tmp-dir slugifier mangles
    # the parametrized name for the dir, so creating a file with the
    # original bytes in there is unreliable.)
    f = tmp_path / evil_name
    url = opener.skim_url(f, page=1)

    # No raw control characters survive into the URL — anything that could
    # break out of an AppleScript string literal or a shell argv element
    # is percent-encoded.
    for forbidden in ("\n", "\r", "\t", '"', "\\"):
        assert forbidden not in url, (
            f"raw {forbidden!r} leaked into URL for filename {evil_name!r}: {url}"
        )

    # The URL still round-trips back to the resolved absolute path.
    fragment_split = url.split("#", 1)[0]
    assert fragment_split.startswith("skim:///")
    decoded = urllib.parse.unquote(fragment_split[len("skim://") :])
    assert decoded == str(f.resolve())


def test_skim_url_rejects_nul_byte_via_path_resolution(tmp_path: Path) -> None:
    """NUL in a Path raises before ``skim_url`` ever runs — Python's path
    layer refuses to embed it, which is the right outcome (NUL would
    truncate an argv element on the C side of ``open(2)``)."""
    with pytest.raises(ValueError, match=r"null byte|embedded null"):
        Path(str(tmp_path / "nul\x00.pdf")).resolve()


def test_open_smart_dispatches_to_url_for_adversarial_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: open_smart never reaches AppleScript regardless of
    filename contents, because the AppleScript path no longer exists.
    A missing ``open_pdf_via_applescript`` symbol on the module would
    AttributeError if anything still routed through it."""
    assert not hasattr(opener, "open_pdf_via_applescript")

    f = tmp_path / 'evil"newline.pdf'
    monkeypatch.setattr(opener, "_has_skim", lambda: True)
    # Isolate from the user's real config — without this the test routes
    # through whichever ``[app_defaults]`` the developer happens to have set.
    from fnd.config import Config

    monkeypatch.setattr("fnd.config.load", lambda *a, **kw: Config())
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        opener,
        "open_pdf_via_url",
        lambda path, page, *, search="": (
            captured.append({"path": str(path), "page": page, "search": search}) or 0
        ),
    )
    opener.open_smart(path=f, kind="pdf", page=3, query="")
    assert len(captured) == 1
    assert captured[0]["page"] == 3


def test_open_strategy_literal_no_longer_contains_applescript() -> None:
    """Confirm the removed enum variant stays removed."""
    import typing

    args = typing.get_args(opener.OpenStrategy)
    assert "applescript" not in args
    assert "url" in args
