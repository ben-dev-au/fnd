"""Apps registry — resolves a (kind, source) hit to an app and dispatches.

Three layers:

* ``BUILTIN_APPS`` — frozen registry of ship-default apps (``system``,
  ``preview``, ``skim``, ``pdf_expert``, ``obsidian``, ``vscode``). Each
  entry has its own ``handler`` closure that constructs an argv or URL,
  validates inputs, and dispatches via :func:`subprocess.run` (never a
  shell).
* :func:`load_user_apps` — turns ``[apps.<id>]`` TOML tables into ``App``
  records whose handlers do pure :py:meth:`str.format` substitution into
  the user's argv or URL template. Mutually-exclusive ``argv``/``url`` is
  enforced; ids and ``handles`` are constrained to safe character sets.
* :func:`resolve_app` walks per-source ``app_for[kind]`` →
  per-source ``app`` → global ``app_defaults[kind]`` → ``system``.

Preview's PDF handler keystrokes "Go to Page" via ``osascript``. When
Accessibility is not granted, :func:`ax_trusted` is False and the handler
falls back to ``open -a Preview <path>`` (no page jump) and notifies the
user once per session via :func:`_emit_notice`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# ── Data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OpenRequest:
    """Everything an app handler might need to open a hit at a position.

    ``vault`` / ``file_in_vault`` are populated only when the source is
    flagged as an Obsidian vault. Empty otherwise.
    """

    path: Path
    kind: str
    page: int = 0
    slide: int = 0
    heading_path: str = ""
    line: int = 0
    query: str = ""
    vault: str = ""
    file_in_vault: str = ""
    source_path: Path | None = None


@dataclass(frozen=True, slots=True)
class App:
    id: str
    display_name: str
    handles: tuple[str, ...]
    handler: Callable[[OpenRequest], int]
    available: Callable[[], bool]
    positional: bool
    notes: str = ""


# Whitelist of file kinds an app may declare in its ``handles``. Keeps user
# TOML from registering arbitrary handlers for arbitrary string keys.
ALLOWED_HANDLES: Final[frozenset[str]] = frozenset(
    {"md", "markdown", "txt", "pdf", "pptx", "docx", "*"}
)

# App ids must be safe for use as Pydantic dict keys and as TOML table keys.
APP_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


# ── Built-in app availability probes ──────────────────────────────────────


def _skim_app_exists() -> bool:
    return any(
        p.exists()
        for p in (
            Path("/Applications/Skim.app"),
            Path.home() / "Applications" / "Skim.app",
        )
    )


def _preview_app_exists() -> bool:
    return any(
        p.exists()
        for p in (
            Path("/System/Applications/Preview.app"),
            Path("/Applications/Preview.app"),
        )
    )


def _pdf_expert_app_exists() -> bool:
    return any(
        p.exists()
        for p in (
            Path("/Applications/PDF Expert.app"),
            Path.home() / "Applications" / "PDF Expert.app",
        )
    )


def _obsidian_app_exists() -> bool:
    return any(
        p.exists()
        for p in (
            Path("/Applications/Obsidian.app"),
            Path.home() / "Applications" / "Obsidian.app",
        )
    )


def _vscode_cli_exists() -> bool:
    return shutil.which("code") is not None


# ── Accessibility probe (cached) ──────────────────────────────────────────

_AX_PROBE_SCRIPT: Final[str] = 'tell application "System Events" to return name of first process'
_ax_cache: dict[str, bool] = {}
_notice_seen: set[str] = set()


def _reset_ax_cache() -> None:  # pyright: ignore[reportUnusedFunction]
    """Clear the AX-trusted cache. Test-only seam used by
    ``tests/test_apps_registry.py`` to isolate the AX cache between
    tests; not part of the public API."""
    _ax_cache.clear()
    _notice_seen.clear()


def _probe_ax_trusted() -> bool:
    """Real AX probe — runs a no-op System Events osascript. Treats a
    non-zero exit code as "not trusted". Slow (~150-300 ms), so the result
    is cached for the lifetime of the process via :func:`ax_trusted`."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", _AX_PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def ax_trusted() -> bool:
    """Return True if the launching process has macOS Accessibility access.

    Cached per process. Tests reset via :func:`_reset_ax_cache`.
    """
    if "value" not in _ax_cache:
        _ax_cache["value"] = _probe_ax_trusted()
    return _ax_cache["value"]


# Pluggable notice sink. TUI registers a callable that pushes the
# AccessibilityPermissionScreen modal (or routes to .notify for other
# kinds). CLI / test callers see stderr via the fallback below.
_notice_sink: Callable[[str], None] | None = None


def set_notice_sink(sink: Callable[[str], None] | None) -> None:
    """Register the notice sink. ``None`` resets to the stderr fallback.

    The TUI uses this to surface :data:`_AX_NOTICE` (and any future
    notices) through an in-app modal, rather than printing under the
    curses display where the user can't see it.
    """
    global _notice_sink
    _notice_sink = sink


def _emit_notice(message: str) -> None:
    """One-shot user-facing notice — dedup'd by message text so the same
    fallback warning never spams the log. Routed through
    :func:`set_notice_sink` when a TUI sink is registered."""
    if message in _notice_seen:
        return
    _notice_seen.add(message)
    if _notice_sink is not None:
        _notice_sink(message)
        return
    import sys

    print(message, file=sys.stderr)


# ── Template rendering ────────────────────────────────────────────────────


_PCT_SAFE: Final[str] = ""  # encode every byte except ASCII alphanumerics + _.~-


def _render_vars(req: OpenRequest) -> dict[str, str]:
    """Template variable bag. Empty fields render as the empty string so
    ``str.format`` won't KeyError on optional placeholders."""
    path_s = str(req.path)
    page_s = str(req.page) if req.page else ""
    slide_s = str(req.slide) if req.slide else ""
    line_s = str(req.line) if req.line else ""

    def pct(s: str) -> str:
        return urllib.parse.quote(s, safe=_PCT_SAFE)

    return {
        "path": path_s,
        "path_pct": pct(path_s),
        "page": page_s,
        "slide": slide_s,
        "line": line_s,
        "heading": req.heading_path,
        "heading_pct": pct(req.heading_path),
        "query": req.query,
        "query_pct": pct(req.query),
        "vault": req.vault,
        "vault_pct": pct(req.vault),
        "file_in_vault": req.file_in_vault,
        "file_in_vault_pct": pct(req.file_in_vault),
    }


def _render_argv(template: list[str], req: OpenRequest) -> list[str]:
    """Substitute placeholders token-by-token. Tokens that resolve to a
    locator suffix (``:<empty>``) get the trailing locator stripped — keeps
    ``code -g <path>:<line>:1`` from devolving into ``code -g <path>::1``
    when ``line`` is unknown."""
    vars_ = _render_vars(req)
    out: list[str] = []
    for token in template:
        rendered = token.format(**vars_)
        # Strip trailing empty locator segments: "path::1" → "path"
        rendered = _strip_empty_locator(rendered)
        out.append(rendered)
    # If the locator argument collapsed to just the path AND the previous
    # token was a goto flag like "-g", drop the flag too — invoking
    # ``code -g <path>`` works but is a useless cosmetic mismatch with the
    # template's intent.
    if len(out) >= 3 and out[-2] in {"-g", "--goto"} and not _has_locator_chars(out[-1]):
        del out[-2]
    return out


def _has_locator_chars(token: str) -> bool:
    """True if ``token`` looks like ``path:line[:col]`` (has at least one
    ``:line``-style numeric segment after the path)."""
    parts = token.rsplit(":", 2)
    return len(parts) >= 2 and any(p.isdigit() and p for p in parts[1:])


_DROP_EMPTY_LOC_RE: Final[re.Pattern[str]] = re.compile(r"::+\d*$")
_DROP_TRAILING_COLON_RE: Final[re.Pattern[str]] = re.compile(r":$")


def _strip_empty_locator(s: str) -> str:
    """Collapse trailing locator segments that represent missing positions.

    * ``path::1``  (line empty, col=1)        → ``path``
    * ``path::``   (line and col empty)       → ``path``
    * ``path:``    (line empty, no col)       → ``path``
    * ``path:42:1`` (line present, col=1)     → unchanged
    """
    s = _DROP_EMPTY_LOC_RE.sub("", s)
    s = _DROP_TRAILING_COLON_RE.sub("", s)
    return s


def _render_url(template: str, req: OpenRequest) -> str:
    return template.format(**_render_vars(req))


# ── Built-in handlers ─────────────────────────────────────────────────────


def _handle_system(req: OpenRequest) -> int:
    return subprocess.run(["open", str(req.path)], check=False).returncode


def _handle_skim(req: OpenRequest) -> int:
    """Defer to the existing ``opener.skim_url`` builder so the URL stays
    bit-for-bit identical to the pre-refactor output. Importing inside the
    function avoids a top-level cycle with ``fnd.opener`` which itself
    will import ``fnd.apps`` after the Phase 1 refactor."""
    from fnd.opener import open_pdf_via_url

    return open_pdf_via_url(req.path, req.page, search=req.query.strip())


# Embedded AppleScript — kept in lockstep with scripts/spike_preview_page_jump.py
# until that spike is deleted. Edits here MUST be mirrored there during the
# spike's lifetime so reliability gains transfer.
_PREVIEW_PAGE_JUMP_SCRIPT: Final[str] = r"""
on run argv
    set pdfPath to item 1 of argv
    set pageNum to item 2 of argv
    tell application "Preview"
        activate
        open POSIX file pdfPath
    end tell
    set tries to 0
    repeat until tries > 30
        try
            tell application "Preview"
                if (exists front document) and (path of front document contains pdfPath) then exit repeat
            end tell
        end try
        delay 0.1
        set tries to tries + 1
    end repeat
    tell application "Preview" to activate
    delay 0.1
    tell application "System Events"
        tell process "Preview"
            keystroke "g" using {option down, command down}
            delay 0.15
            keystroke pageNum
            delay 0.05
            key code 36
        end tell
    end tell
end run
"""


_AX_NOTICE: Final[str] = (
    "Preview page-jump needs macOS Accessibility access. "
    "Grant it in System Settings → Privacy & Security → Accessibility for the app "
    "you launched fnd from, then press `o` again. "
    "Falling back to opening the PDF on page 1 for this hit."
)


def _handle_preview(req: OpenRequest) -> int:
    """Preview PDF opener. With a page locator AND AX granted, runs the
    embedded keystroke AppleScript. Otherwise opens the PDF on page 1 via
    ``open -a Preview <path>`` and emits a one-shot Accessibility notice."""
    if req.page <= 0 or not ax_trusted():
        if req.page > 0 and not ax_trusted():
            _emit_notice(_AX_NOTICE)
        return subprocess.run(["open", "-a", "Preview", str(req.path)], check=False).returncode
    return subprocess.run(
        ["osascript", "-e", _PREVIEW_PAGE_JUMP_SCRIPT, str(req.path), str(req.page)],
        check=False,
    ).returncode


def _handle_pdf_expert(req: OpenRequest) -> int:
    """Open the PDF in PDF Expert.

    Earlier I tried a ``pdf-expert-7://`` URL — that scheme isn't
    registered (the bundle's Info.plist exposes ``pdfexpert://`` only)
    and any URL it does accept isn't publicly documented in a stable
    form. ``open -a "PDF Expert" <path>`` is the reliable invocation
    on macOS: it always opens the file, but it has NO page-jump (PDF
    Expert on Mac doesn't expose a documented page-locator entry
    point). Users who want page-jump should pick Skim or Preview.
    """
    return subprocess.run(["open", "-a", "PDF Expert", str(req.path)], check=False).returncode


_HEADING_BREADCRUMB_SEP: Final[str] = " > "


def _heading_path_to_anchor(heading_path: str) -> str:
    """Convert a chunk's ``heading_path`` ("A > B > C") to Obsidian's
    nested-heading anchor format ("A#B#C").

    Obsidian's wiki-link syntax uses ``#`` between successive heading
    levels — ``[[Note#A#B#C]]`` navigates to the C heading inside B
    inside A. The chained form is more specific than the leaf alone:
    if the file has multiple headings literally named "C", only the one
    nested under A→B will match.
    """
    parts = [p.strip() for p in heading_path.split(_HEADING_BREADCRUMB_SEP) if p.strip()]
    return "#".join(parts)


_ADVANCED_URI_PLUGIN_DIR: Final[str] = "obsidian-advanced-uri"


def _advanced_uri_available(source_path: Path | None) -> bool:
    """True when the Advanced URI plugin is installed in the vault that
    contains ``source_path``.

    Walks up from ``source_path`` looking for a ``.obsidian/plugins/<id>``
    directory — the same detection pattern :func:`detect_obsidian_vault`
    uses for the vault root, just one level deeper. Returns ``False``
    when ``source_path`` is unset, isn't under any vault, or the plugin
    folder is missing. Cheap (~one ``stat`` per parent walked).
    """
    if source_path is None:
        return False
    p = source_path.expanduser().resolve()
    if p.is_file():
        p = p.parent
    while True:
        candidate = p / ".obsidian" / "plugins" / _ADVANCED_URI_PLUGIN_DIR
        if candidate.is_dir():
            return True
        if (p / ".obsidian").is_dir():
            return False  # found the vault but no plugin → stop walking
        if p.parent == p:
            return False
        p = p.parent


_WORD_TOKEN_RE: Final = re.compile(r"\w+")


def _resolve_match_line(
    path: Path, query: str, from_line: int, *, max_scan_lines: int = 4000
) -> int:
    """Scan ``path`` from ``from_line`` for the first line whose lowercased
    content contains any whole-word token of ``query``. Returns the matched
    line (1-based) or ``from_line`` on miss.

    Used by deep-link handlers that support line jumps (Obsidian
    Advanced URI, VS Code) so the user lands on the matched word
    rather than the chunk's heading. The chunk's ``line`` is the
    heading line — a long section can drop the user 50+ lines above
    their match without this resolution step.

    Conservative on fuzzy: only literal whole-word query tokens are
    matched, so a query like ``template~2`` matching the file's
    ``templating`` falls back to the chunk start. That's
    intentional — the goal is to *improve precision when we can*,
    not guarantee it.
    """
    if not query or from_line < 1:
        return from_line
    terms = {m.group(0).lower() for m in _WORD_TOKEN_RE.finditer(query)}
    if not terms:
        return from_line
    try:
        with path.open("r", encoding="utf-8") as f:
            for offset, raw in enumerate(f):
                if offset + 1 < from_line:
                    continue
                if offset + 1 - from_line >= max_scan_lines:
                    break
                low = raw.lower()
                if any(m.group(0).lower() in terms for m in _WORD_TOKEN_RE.finditer(low)):
                    return offset + 1
    except (OSError, UnicodeDecodeError):
        return from_line
    return from_line


def _handle_obsidian(req: OpenRequest) -> int:
    """Obsidian deep-link with optional line-precise jump via the
    Advanced URI plugin.

    URL form picked by what's available:

    * **Advanced URI + line known** (``line > 0`` and the vault has the
      ``obsidian-advanced-uri`` plugin installed): uses
      ``obsidian://advanced-uri?vault=X&filepath=Y&line=N``. This is
      the only path that lands the cursor on the *matched line* rather
      than at the top of the section. Built-in URL forms can only target
      headings, so a match deep inside a long section drops the user
      lines above their match — see :func:`_advanced_uri_available`.
    * **Vault set, no plugin (or no line)**: ``obsidian://open?vault=X
      &file=Y#A#B#C``. Heading anchor uses Obsidian's chained-heading
      syntax via :func:`_heading_path_to_anchor`.
    * **No vault**: ``obsidian://open?path=<abs>[#A#B#C]``. Obsidian
      routes the path to whichever vault contains it. We never fall
      back to ``open <path>`` — the user explicitly picked Obsidian and
      a silent app-swap is misleading.
    """
    use_advanced = req.line > 0 and _advanced_uri_available(req.source_path)
    if req.vault and use_advanced:
        filepath = req.file_in_vault or str(req.path)
        # Resolve the chunk's heading line forward to the actual matched
        # line when a query is in play — Advanced URI's ``line=N`` jumps
        # the cursor to exactly that line, so we get word-precision
        # instead of section-precision.
        target_line = _resolve_match_line(req.path, req.query, req.line)
        # ``openmode=tab`` opens the match in a NEW tab instead of navigating
        # the active one (which silently loses whatever the user had open).
        # Current Advanced URI param — supersedes the deprecated ``newpane``.
        url = (
            "obsidian://advanced-uri"
            f"?vault={urllib.parse.quote(req.vault, safe=_PCT_SAFE)}"
            f"&filepath={urllib.parse.quote(filepath, safe=_PCT_SAFE)}"
            f"&line={target_line}"
            "&openmode=tab"
        )
    elif req.vault:
        file_param = req.file_in_vault or str(req.path)
        if req.heading_path:
            file_param = f"{file_param}#{_heading_path_to_anchor(req.heading_path)}"
        url = (
            "obsidian://open"
            f"?vault={urllib.parse.quote(req.vault, safe=_PCT_SAFE)}"
            f"&file={urllib.parse.quote(file_param, safe=_PCT_SAFE)}"
        )
    else:
        path_str = str(req.path)
        if req.heading_path:
            path_str = f"{path_str}#{_heading_path_to_anchor(req.heading_path)}"
        url = f"obsidian://open?path={urllib.parse.quote(path_str, safe=_PCT_SAFE)}"
    return subprocess.run(["open", url], check=False).returncode


def _handle_vscode(req: OpenRequest) -> int:
    """``code -g <path>:<line>:1`` when ``line`` is known; ``code <path>``
    otherwise. Same handler used for md / txt / fallback."""
    argv = ["code", "-g", f"{req.path}:{req.line}:1"] if req.line > 0 else ["code", str(req.path)]
    return subprocess.run(argv, check=False).returncode


# ── Built-in registry ─────────────────────────────────────────────────────


BUILTIN_APPS: Final[dict[str, App]] = {
    "system": App(
        id="system",
        display_name="System Default",
        handles=("*",),
        handler=_handle_system,
        available=lambda: True,
        positional=False,
        notes="LaunchServices default — never deep-links to a page or line.",
    ),
    "preview": App(
        id="preview",
        display_name="Preview",
        handles=("pdf",),
        handler=_handle_preview,
        # Late-binding lookups so tests can monkeypatch _*_exists helpers.
        available=lambda: _preview_app_exists(),
        positional=True,
        notes="Page-jump via Cmd-Opt-G keystroke automation; requires Accessibility.",
    ),
    "skim": App(
        id="skim",
        display_name="Skim",
        handles=("pdf",),
        handler=_handle_skim,
        available=lambda: _skim_app_exists(),
        positional=True,
        notes="skim:// URL with #page=N&search=Q; highlights matched text.",
    ),
    "pdf_expert": App(
        id="pdf_expert",
        display_name="PDF Expert",
        handles=("pdf",),
        handler=_handle_pdf_expert,
        available=lambda: _pdf_expert_app_exists(),
        positional=False,  # opens via `open -a`; no documented page-jump
        notes="open -a 'PDF Expert' <path> — no page-jump on macOS.",
    ),
    "obsidian": App(
        id="obsidian",
        display_name="Obsidian",
        handles=("md", "markdown"),
        handler=_handle_obsidian,
        available=lambda: _obsidian_app_exists(),
        positional=True,
        notes=(
            "Install the Advanced URI plugin in your vault for "
            "line-precise jumps; without it lands at the section heading."
        ),
    ),
    "vscode": App(
        id="vscode",
        display_name="VS Code",
        handles=("md", "markdown", "txt", "*"),
        handler=_handle_vscode,
        available=lambda: _vscode_cli_exists(),
        positional=True,
        notes="`code -g <path>:<line>:1`; requires the `code` CLI in PATH.",
    ),
}


# ── User-app loader ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _UserAppSpec:
    """Validated user-app shape used to build a runtime App. Distinct from
    AppConfig (pydantic, lives in fnd/config.py) so apps.py stays
    pydantic-free and importable from anywhere."""

    id: str
    display_name: str
    handles: tuple[str, ...]
    argv: tuple[str, ...] | None
    url: str | None


def _validate_user_app(app_id: str, raw: dict[str, Any]) -> _UserAppSpec:
    if not APP_ID_RE.fullmatch(app_id):
        raise ValueError(f"invalid app id {app_id!r}: must match {APP_ID_RE.pattern}")
    display_name = raw.get("display_name") or app_id
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError(f"app {app_id!r}: display_name must be a non-empty string")
    handles_raw = raw.get("handles")
    if not isinstance(handles_raw, list) or not handles_raw:
        raise ValueError(f"app {app_id!r}: handles must be a non-empty list")
    handles: list[str] = []
    for h in handles_raw:
        if h not in ALLOWED_HANDLES:
            raise ValueError(
                f"app {app_id!r}: unknown handle kind {h!r} (allowed: {sorted(ALLOWED_HANDLES)})"
            )
        handles.append(h)
    argv_raw = raw.get("argv")
    url_raw = raw.get("url")
    if (argv_raw is None) == (url_raw is None):
        raise ValueError(f"app {app_id!r}: exactly one of argv or url must be set")
    argv: tuple[str, ...] | None = None
    if argv_raw is not None:
        if not isinstance(argv_raw, list) or not all(isinstance(x, str) for x in argv_raw):
            raise ValueError(f"app {app_id!r}: argv must be a list of strings")
        argv = tuple(argv_raw)
    url: str | None = None
    if url_raw is not None:
        if not isinstance(url_raw, str) or not url_raw:
            raise ValueError(f"app {app_id!r}: url must be a non-empty string")
        url = url_raw
    # Dry-run the template against a stub OpenRequest so placeholder
    # typos ({ptha} for {path}) surface at config load — not at open
    # time, where the resulting KeyError gets surfaced as an ugly
    # "Open failed: KeyError: 'ptha'" toast in the OpenWithScreen.
    _validate_template(app_id, argv=argv, url=url)
    return _UserAppSpec(
        id=app_id,
        display_name=display_name,
        handles=tuple(handles),
        argv=argv,
        url=url,
    )


def _validate_template(
    app_id: str,
    *,
    argv: tuple[str, ...] | None,
    url: str | None,
) -> None:
    """Dry-run-format the template against a fully-populated stub
    OpenRequest. Re-raise the placeholder name as a ValueError so the
    config-load error message points users at the typo."""
    stub = OpenRequest(
        path=Path("/stub/path"),
        kind="md",
        page=1,
        slide=1,
        heading_path="Stub > Section",
        line=1,
        query="stub",
        vault="StubVault",
        file_in_vault="stub.md",
        source_path=Path("/stub"),
    )
    try:
        if argv is not None:
            _render_argv(list(argv), stub)
        if url is not None:
            _render_url(url, stub)
    except KeyError as e:
        # e.args[0] is the missing placeholder name.
        bad = e.args[0] if e.args else "?"
        raise ValueError(
            f"app {app_id!r}: template references unknown placeholder {{{bad}}}. "
            f"See docs/apps/README.md for the variable list."
        ) from None


def _make_user_handler(spec: _UserAppSpec) -> Callable[[OpenRequest], int]:
    if spec.argv is not None:
        argv_template = list(spec.argv)

        def _run_argv(req: OpenRequest) -> int:
            return subprocess.run(_render_argv(argv_template, req), check=False).returncode

        return _run_argv
    assert spec.url is not None  # exclusivity enforced above
    url_template = spec.url

    def _run_url(req: OpenRequest) -> int:
        url = _render_url(url_template, req)
        return subprocess.run(["open", url], check=False).returncode

    return _run_url


def load_user_apps(raw_apps: dict[str, dict[str, Any]] | None) -> dict[str, App]:
    """Build an ``App`` per ``[apps.<id>]`` table. Raises ``ValueError`` on
    the first invalid entry — partial registries hide bugs."""
    out: dict[str, App] = {}
    if not raw_apps:
        return out
    for app_id, raw in raw_apps.items():
        spec = _validate_user_app(app_id, raw)
        out[app_id] = App(
            id=spec.id,
            display_name=spec.display_name,
            handles=spec.handles,
            handler=_make_user_handler(spec),
            available=lambda: True,  # user-declared — assume present
            positional="{page}" in (spec.url or "")
            or any("{line}" in t or "{page}" in t for t in (spec.argv or ())),
        )
    return out


def build_registry(cfg: Any) -> dict[str, App]:
    """Combine BUILTIN_APPS with the user-defined apps on ``cfg``. User
    entries with the same id as a built-in win — gives users a way to
    override a built-in's template without touching code."""
    user_raw = getattr(cfg, "apps", None) or {}
    user_built = load_user_apps(
        {k: (v if isinstance(v, dict) else dict(v.__dict__)) for k, v in user_raw.items()}
    )
    merged: dict[str, App] = dict(BUILTIN_APPS)
    merged.update(user_built)
    return merged


# ── Resolver ──────────────────────────────────────────────────────────────


def resolve_app(
    *,
    kind: str,
    source: Any | None,
    app_defaults: dict[str, str],
    registry: dict[str, App],
) -> App:
    """Walk the lookup hierarchy and return the resolved app.

    1. ``source.app_for[kind]`` if set, if the id exists in registry.
    2. ``source.app`` if set, if it exists, and if ``kind in app.handles``.
    3. ``app_defaults[kind]`` if set and the id exists.
    4. ``registry["system"]``.

    Unknown ids at any layer are silently skipped (treated as absent) so a
    typo in a single source's config doesn't block the open entirely.
    """
    if source is not None:
        per_source_for = getattr(source, "app_for", None) or {}
        chosen_id = per_source_for.get(kind)
        if chosen_id and chosen_id in registry:
            return registry[chosen_id]
        per_source_app = getattr(source, "app", None)
        if per_source_app and per_source_app in registry:
            app = registry[per_source_app]
            if kind in app.handles or "*" in app.handles:
                return app
    default_id = app_defaults.get(kind)
    if default_id and default_id in registry:
        return registry[default_id]
    return registry["system"]


# ── Vault detection (Phase 2 helper, lives here so opener can use it) ─────


def detect_obsidian_vault_path(path: Path) -> Path | None:
    """Walk up from ``path`` looking for the directory containing a
    ``.obsidian/`` folder. Return the absolute path to that vault
    root, or ``None`` if no vault is found before the filesystem root.

    Used by the opener to compute ``file_in_vault`` relative to the
    actual vault root — which Obsidian's Advanced URI expects — even
    when the source's ``path`` is a subdirectory of the vault (a
    common layout for collections that index only one course's notes
    out of a larger vault).
    """
    p = path.expanduser().resolve()
    if p.is_file():
        p = p.parent
    while True:
        if (p / ".obsidian").is_dir():
            return p
        if p.parent == p:
            return None
        p = p.parent


def detect_obsidian_vault(path: Path) -> str | None:
    """Walk up from ``path`` and return the basename of the containing
    vault, or ``None`` when no vault is above ``path``.

    Used by the Settings TUI to pre-fill ``app_params.vault`` when the
    user picks Obsidian as a source's app. For the vault's filesystem
    path (needed by the deep-link layer) use
    :func:`detect_obsidian_vault_path`.
    """
    root = detect_obsidian_vault_path(path)
    return root.name if root is not None else None


__all__ = [
    "ALLOWED_HANDLES",
    "APP_ID_RE",
    "BUILTIN_APPS",
    "App",
    "OpenRequest",
    "ax_trusted",
    "build_registry",
    "detect_obsidian_vault",
    "detect_obsidian_vault_path",
    "load_user_apps",
    "resolve_app",
    "set_notice_sink",
]
