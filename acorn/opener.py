"""Open-in-app dispatch.

Per plan §5 + §17 + §21 Spike C:

* PDF: AppleScript (primary, robust under unusual filenames). URL-scheme fallback
  for shareable links: ``skim:///<percent-encoded-absolute-path>#page=N``.
* PPTX / DOCX / MD / TXT: ``open <file>`` (LaunchServices default app) — these
  formats have no reliable page-jump protocol on macOS; the TUI surfaces the
  slide/heading in its footer so the user can scroll to it manually.

For Quick Look peek (``qlmanage -p``), use :func:`peek`. Quick Look has no
documented page-jump argument; peek shows the file's first page only and is
intended as a "is this the right document" gut check.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Final, Literal

OpenStrategy = Literal["applescript", "url", "default"]

DEFAULT_PDF_STRATEGY: Final[OpenStrategy] = "applescript"


def _has_skim() -> bool:
    """Return True if Skim.app is installed at one of the standard locations."""
    candidates = (
        Path("/Applications/Skim.app"),
        Path.home() / "Applications" / "Skim.app",
    )
    return any(p.exists() for p in candidates)


def skim_url(path: Path, page: int, *, search: str = "") -> str:
    """Build a Skim deep-link URL for ``path`` at 1-based ``page``.

    When ``search`` is non-empty, Skim opens with that string highlighted /
    selected on the page (verified during plan §21 Spike C — Skim's URL
    fragment supports ``&search=…``).

    Format: ``skim:///<pct-encoded-abs-path>#page=N`` with three slashes
    (skim:// + absolute path starting with /).
    """
    abs_path = str(path.expanduser().resolve())
    encoded_path = urllib.parse.quote(abs_path, safe="/")
    fragment_parts = [f"page={page}"]
    if search:
        fragment_parts.append(f"search={urllib.parse.quote(search)}")
    return f"skim://{encoded_path}#{'&'.join(fragment_parts)}"


def _osascript(commands: list[str]) -> subprocess.CompletedProcess[bytes]:
    args: list[str] = ["osascript"]
    for c in commands:
        args.extend(["-e", c])
    return subprocess.run(args, check=False, capture_output=True)


def open_pdf_via_applescript(path: Path, page: int) -> int:
    """Open ``path`` in Skim and jump to 1-based ``page``. Returns the
    exit code of the underlying ``osascript`` invocation."""
    posix_path = str(path.expanduser().resolve())
    # Backslash + double-quote escapes for AppleScript string literals.
    escaped = posix_path.replace("\\", "\\\\").replace('"', '\\"')
    proc = _osascript(
        [
            'tell application "Skim" to activate',
            f'tell application "Skim" to open POSIX file "{escaped}"',
            f'tell application "Skim" to tell document 1 to go to page {int(page)}',
        ]
    )
    return proc.returncode


def open_pdf_via_url(path: Path, page: int, *, search: str = "") -> int:
    """Open the Skim URL via ``open``. The URL form supports ``&search=`` so
    the match is highlighted on the page; AppleScript does not."""
    url = skim_url(path, page, search=search)
    return subprocess.run(["open", url], check=False).returncode


def open_default(path: Path) -> int:
    """``open <path>`` — LaunchServices picks the default app for the type."""
    return subprocess.run(["open", str(path)], check=False).returncode


def reveal_in_finder(path: Path) -> int:
    """``open -R <path>`` — reveal in Finder, no app launch."""
    return subprocess.run(["open", "-R", str(path)], check=False).returncode


def peek(path: Path) -> int:
    """Quick Look preview as a side window. Quick Look cannot deep-link to a
    page; this is for quick "is this the right doc" checks. Returns the
    ``qlmanage`` exit code."""
    if shutil.which("qlmanage") is None:
        return 127
    # `-p` prints to stdout while showing the preview window.
    return subprocess.run(["qlmanage", "-p", str(path)], check=False).returncode


def open_smart(
    *,
    path: Path,
    kind: str,
    page: int = 0,
    query: str = "",
    pdf_strategy: OpenStrategy = DEFAULT_PDF_STRATEGY,
) -> int:
    """Dispatch based on ``kind`` and locator metadata.

    PDFs deep-link via Skim. When ``query`` is non-empty, the URL form is
    preferred over AppleScript because only the URL form supports
    ``&search=`` (which makes Skim highlight the matching string on
    the opened page). Falls back to AppleScript for query-less opens
    (URL form has slight encoding fragility under unusual filenames per
    §21 Spike C). Non-PDF kinds fall through to ``open <path>``.
    """
    if kind == "pdf" and page > 0 and _has_skim():
        # Prefer URL when we have a search term — only that form highlights.
        if query.strip():
            return open_pdf_via_url(path, page, search=query.strip())
        if pdf_strategy == "url":
            return open_pdf_via_url(path, page)
        if pdf_strategy == "applescript":
            return open_pdf_via_applescript(path, page)
    return open_default(path)


# ── Diagnostics for the TUI status bar ──────────────────────────────────────


def explain_open(*, kind: str, page: int, pdf_strategy: OpenStrategy) -> str:
    """Human-readable description of what ``open_smart`` will do."""
    if kind == "pdf" and page > 0 and _has_skim():
        if pdf_strategy == "applescript":
            return f"AppleScript → Skim, page {page}"
        if pdf_strategy == "url":
            return f"open '{shlex.quote(str(skim_url(Path('/X'), page)))}'"
    return "open <file> (default app)"


def reveal(path: Path | str) -> None:
    """Reveal ``path`` in Finder (selected) via macOS `open -R`.

    Fire-and-forget — uses Popen so the TUI doesn't block on Finder's
    launch latency. On non-macOS platforms this is a no-op for now (the
    project targets macOS per pyproject).
    """
    import platform

    if platform.system() != "Darwin":
        return
    subprocess.Popen(
        ["open", "-R", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
