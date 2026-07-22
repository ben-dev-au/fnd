"""Open-in-app dispatch.

* PDF: Skim URL scheme — ``skim:///<percent-encoded-absolute-path>#page=N``
  — handed to ``open <url>``. ``urllib.parse.quote`` covers every byte
  including filename newlines/quotes that could otherwise inject into
  a shell-out or AppleScript string literal.
* PPTX / DOCX / MD / TXT: ``open <file>`` (LaunchServices default app) — these
  formats have no reliable page-jump protocol on macOS; the TUI surfaces the
  slide/heading in its footer so the user can scroll to it manually.
"""

from __future__ import annotations

import shlex
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Final, Literal

from fnd import launcher

OpenStrategy = Literal["url", "default"]

DEFAULT_PDF_STRATEGY: Final[OpenStrategy] = "url"


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


def open_pdf_via_url(path: Path, page: int, *, search: str = "") -> int:
    """Open the Skim URL via ``open``. The URL form supports ``&search=`` so
    the match is highlighted on the page; AppleScript does not."""
    url = skim_url(path, page, search=search)
    return launcher.open_url(url)


def open_default(path: Path) -> int:
    """Open ``path`` in the OS default app for its type (via the launcher)."""
    return launcher.open_path(path)


def reveal_in_finder(path: Path) -> int:
    """Reveal ``path`` in the platform file manager, no app launch."""
    launcher.reveal(path)
    return 0


def open_smart(
    *,
    path: Path,
    kind: str,
    page: int = 0,
    page_label: str = "",
    query: str = "",
    pdf_strategy: OpenStrategy = DEFAULT_PDF_STRATEGY,
    source: Any | None = None,
    slide: int = 0,
    heading_path: str = "",
    line: int = 0,
) -> int:
    """Dispatch the focused hit to the resolved app.

    Walks the apps registry hierarchy:

      1. ``source.app_for[kind]`` (set per-source per-filetype)
      2. ``source.app`` if its ``handles`` covers ``kind``
      3. ``Config.app_defaults[kind]``
      4. The ``system`` built-in (``open <path>``).

    ``pdf_strategy = "default"`` is preserved for back-compat — it forces
    the ``system`` handler for the current call regardless of the
    resolved app. Always-default callers should use :func:`open_default`.
    """
    if pdf_strategy == "default":
        return open_default(path)

    from fnd import apps as apps_mod
    from fnd.config import load as load_config

    try:
        cfg = load_config()
    except Exception:
        cfg = None

    registry = apps_mod.build_registry(cfg) if cfg is not None else apps_mod.BUILTIN_APPS
    app_defaults: dict[str, str] = dict(getattr(cfg, "app_defaults", {})) if cfg else {}
    # Auto-promote a page-jump-capable PDF app when the user hasn't set
    # one explicitly. Preference order is UX-driven:
    #   1. Skim   - silent URL scheme, no permissions, polished
    #   2. Preview - osascript Go-to-Page keystroke; needs Accessibility,
    #                shows a brief dialog flash but no install required
    #   3. system - LaunchServices fallback; opens at page 1
    # Any explicit ``app_defaults.pdf = "..."`` in the user config wins
    # over this auto-promotion.
    if "pdf" not in app_defaults:
        if sys.platform == "darwin":
            if _has_skim():
                app_defaults["pdf"] = "skim"
            elif apps_mod.BUILTIN_APPS["preview"].available() and apps_mod.ax_trusted():
                app_defaults["pdf"] = "preview"
        else:
            # Linux / Windows: promote the first available page-jump viewer.
            # Each id self-gates by OS via ``available`` (zathura/okular on
            # Linux, sumatra on Windows), so one order serves both.
            for pdf_id in ("zathura", "okular", "sumatra"):
                candidate = registry.get(pdf_id)
                if candidate is not None and candidate.available():
                    app_defaults["pdf"] = pdf_id
                    break

    app_params: dict[str, str] = {}
    if source is not None:
        app_params = dict(getattr(source, "app_params", {}) or {})

    # ``file_in_vault`` MUST be relative to the vault root (the dir
    # containing ``.obsidian/``), not the source's ``path``. A source
    # configured as a subdirectory of the vault — e.g. an Obsidian
    # collection that indexes only one course's notes — would otherwise
    # produce a vault-relative path that's missing the prefix between
    # vault root and source root, and Obsidian's Advanced URI would
    # create a new file at the wrong location. Fall back to source-
    # relative when no vault is found (handler tolerates this).
    source_path = getattr(source, "path", None) if source is not None else None
    vault_root: Path | None = None
    if app_params.get("vault"):
        vault_root = apps_mod.detect_obsidian_vault_path(path)
    req = apps_mod.OpenRequest(
        path=path,
        kind=kind,
        page=page,
        page_label=page_label,
        slide=slide,
        heading_path=heading_path,
        line=line,
        query=query,
        vault=app_params.get("vault", ""),
        file_in_vault=_relative_to(path, vault_root or source_path),
        source_path=source_path,
    )
    app = apps_mod.resolve_app(
        kind=kind,
        source=source,
        app_defaults=app_defaults,
        registry=registry,
    )
    return app.handler(req)


def _relative_to(target: Path, root: Path | None) -> str:
    """Path of ``target`` relative to ``root``, or ``""`` when root is unset
    or unreachable. Used to fill the ``file_in_vault`` template variable
    for Obsidian (which wants paths relative to the vault root)."""
    if root is None:
        return ""
    try:
        return str(target.expanduser().resolve().relative_to(Path(root).expanduser().resolve()))
    except (ValueError, OSError):
        return ""


# ── Diagnostics for the TUI status bar ──────────────────────────────────────


def explain_open(*, kind: str, page: int, pdf_strategy: OpenStrategy) -> str:
    """Human-readable description of what ``open_smart`` will do."""
    if kind == "pdf" and page > 0 and _has_skim() and pdf_strategy == "url":
        return f"open '{shlex.quote(str(skim_url(Path('/X'), page)))}'"
    return "open <file> (default app)"


def reveal(path: Path | str) -> None:
    """Reveal ``path`` in the platform file manager, selecting it where
    supported. Fire-and-forget so the TUI doesn't block on launch latency;
    delegates to the OS launcher (macOS ``open -R`` · Windows
    ``explorer /select,`` · Linux file-manager ``--select`` → folder)."""
    launcher.reveal(path)
