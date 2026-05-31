"""macOS Preview's "Go to Page" navigates by the PDF's printed page LABEL,
not the physical index. The Preview handler must keystroke ``page_label``
when present (falling back to the physical page for label-less PDFs), or a
book with front matter lands the user pages late.

Regression: opening the Design Patterns book's State/Consequences match
(physical page 327, printed label "307") keystroked "327" and landed on
physical 347 (the page printed "327").
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import fnd.apps as apps


def _run_preview(req: apps.OpenRequest) -> list[str]:
    """Invoke the Preview handler with AX granted + open/osascript stubbed,
    and return the argv of the page-jump osascript call."""
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return mock.Mock(returncode=0)

    with (
        mock.patch.object(apps, "ax_trusted", return_value=True),
        mock.patch.object(apps.subprocess, "run", side_effect=fake_run),
    ):
        apps._handle_preview(req)

    jump = [c for c in calls if c and c[0] == "osascript"]
    assert jump, f"no osascript page-jump call; calls={calls}"
    return jump[-1]


def test_preview_jumps_by_page_label_when_present() -> None:
    req = apps.OpenRequest(
        path=Path("/tmp/book.pdf"), kind="pdf", page=327, page_label="307"
    )
    argv = _run_preview(req)
    # Last positional arg to the AppleScript is the page token.
    assert argv[-1] == "307", f"expected label 307, got {argv[-1]!r}"
    assert argv[-1] != "327"


def test_preview_falls_back_to_physical_page_without_label() -> None:
    req = apps.OpenRequest(path=Path("/tmp/plain.pdf"), kind="pdf", page=12)
    argv = _run_preview(req)
    assert argv[-1] == "12"
