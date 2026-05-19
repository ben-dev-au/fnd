"""Phase 1: apps registry, resolver, and template handlers.

These tests pin the contract for ``fnd.apps``:

* ``BUILTIN_APPS`` exposes the expected ids and handler shapes.
* ``resolve_app`` walks per-source → per-source default → global default →
  ``system`` in that order, and skips a per-source app that doesn't handle
  the requested kind.
* User-defined ``[apps.<id>]`` tables build runnable handlers via pure
  template substitution; mutually-exclusive ``argv`` / ``url`` is enforced.
* No template handler ever spawns a shell — every dispatch goes through
  ``subprocess.run`` with an argv list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd import apps
from fnd.apps import OpenRequest, build_registry, load_user_apps, resolve_app

# ── Built-in registry shape ────────────────────────────────────────────────


def test_builtin_apps_ids_present() -> None:
    expected = {"system", "preview", "skim", "pdf_expert", "obsidian", "vscode"}
    assert expected <= set(apps.BUILTIN_APPS), apps.BUILTIN_APPS.keys()


def test_system_app_is_always_available() -> None:
    assert apps.BUILTIN_APPS["system"].available() is True


def test_system_handles_wildcard() -> None:
    assert "*" in apps.BUILTIN_APPS["system"].handles


def test_skim_handles_pdf_only() -> None:
    assert apps.BUILTIN_APPS["skim"].handles == ("pdf",)


def test_obsidian_handles_markdown_variants() -> None:
    h = apps.BUILTIN_APPS["obsidian"].handles
    assert "md" in h
    assert "markdown" in h


def test_vscode_handles_text_kinds_and_wildcard() -> None:
    h = apps.BUILTIN_APPS["vscode"].handles
    assert {"md", "markdown", "txt", "*"} <= set(h)


def test_skim_available_honors_has_skim_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps, "_skim_app_exists", lambda: False)
    assert apps.BUILTIN_APPS["skim"].available() is False
    monkeypatch.setattr(apps, "_skim_app_exists", lambda: True)
    assert apps.BUILTIN_APPS["skim"].available() is True


# ── Resolver hierarchy ────────────────────────────────────────────────────


def _src(**overrides: Any) -> Any:
    """Build a stand-in source object with the fields the resolver reads."""
    from types import SimpleNamespace

    base = {"app": None, "app_for": {}, "app_params": {}, "path": Path("/tmp/x")}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_resolve_picks_per_source_app_for_filetype() -> None:
    registry = apps.BUILTIN_APPS
    src = _src(app_for={"pdf": "skim", "md": "obsidian"})
    chosen = resolve_app(kind="pdf", source=src, app_defaults={}, registry=registry)
    assert chosen.id == "skim"


def test_resolve_falls_back_to_per_source_app_when_handles_match() -> None:
    registry = apps.BUILTIN_APPS
    src = _src(app="obsidian")
    chosen = resolve_app(kind="md", source=src, app_defaults={}, registry=registry)
    assert chosen.id == "obsidian"


def test_resolve_skips_per_source_app_when_kind_not_handled() -> None:
    registry = apps.BUILTIN_APPS
    src = _src(app="obsidian")  # obsidian handles md/markdown, not pdf
    chosen = resolve_app(kind="pdf", source=src, app_defaults={"pdf": "skim"}, registry=registry)
    assert chosen.id == "skim"


def test_resolve_falls_back_to_global_app_defaults() -> None:
    registry = apps.BUILTIN_APPS
    chosen = resolve_app(kind="pdf", source=None, app_defaults={"pdf": "skim"}, registry=registry)
    assert chosen.id == "skim"


def test_resolve_falls_back_to_system_when_nothing_matches() -> None:
    registry = apps.BUILTIN_APPS
    chosen = resolve_app(kind="pptx", source=None, app_defaults={}, registry=registry)
    assert chosen.id == "system"


def test_resolve_ignores_unknown_app_ids_in_per_source_fields() -> None:
    """An app_for entry pointing at a non-existent id is treated as absent."""
    registry = apps.BUILTIN_APPS
    src = _src(app_for={"pdf": "ghost_app"})
    chosen = resolve_app(kind="pdf", source=src, app_defaults={"pdf": "skim"}, registry=registry)
    assert chosen.id == "skim"


# ── Template substitution ─────────────────────────────────────────────────


def test_render_argv_substitutes_per_token() -> None:
    req = OpenRequest(path=Path("/tmp/a b.md"), kind="md", line=42, page=0, query="hello world")
    out = apps._render_argv(["code", "-g", "{path}:{line}:1"], req)
    assert out == ["code", "-g", "/tmp/a b.md:42:1"]


def test_render_argv_omits_line_segment_when_line_zero() -> None:
    req = OpenRequest(path=Path("/tmp/a.txt"), kind="txt", line=0)
    out = apps._render_argv(["code", "-g", "{path}:{line}:1"], req)
    # No useful line locator → drop the locator argument entirely.
    assert out == ["code", "/tmp/a.txt"]


def test_render_url_percent_encodes_path_and_query() -> None:
    req = OpenRequest(path=Path("/tmp/a & b.pdf"), kind="pdf", page=7, query="cat & dog")
    url = apps._render_url("skim://{path_pct}#page={page}&search={query_pct}", req)
    assert "%26" in url  # & encoded in both path and query
    assert "page=7" in url


def test_render_url_drops_empty_placeholders() -> None:
    """Placeholders for empty fields render as the empty string — callers
    that care about a clean URL should design templates accordingly."""
    req = OpenRequest(path=Path("/tmp/x.md"), kind="md", heading_path="")
    url = apps._render_url("obsidian://open?vault={vault_pct}&file={path_pct}", req)
    assert "vault=&" in url


# ── User-defined apps (load_user_apps) ────────────────────────────────────


def _user_app_cfg(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "display_name": "Custom",
        "handles": ["md"],
        "argv": ["myapp", "{path}"],
    }
    base.update(overrides)
    return base


def test_load_user_apps_builds_app_from_argv(tmp_path: Path) -> None:
    cfg = {"custom_md": _user_app_cfg()}
    built = load_user_apps(cfg)
    assert "custom_md" in built
    app = built["custom_md"]
    assert app.display_name == "Custom"
    assert "md" in app.handles


def test_load_user_apps_rejects_argv_and_url_both(tmp_path: Path) -> None:
    cfg = {
        "broken": _user_app_cfg(
            argv=["x"],
            url="custom://{path_pct}",  # both set — invalid
        )
    }
    with pytest.raises(ValueError, match=r"argv|url"):
        load_user_apps(cfg)


def test_load_user_apps_rejects_neither_argv_nor_url() -> None:
    cfg = {"broken": {"display_name": "X", "handles": ["md"]}}
    with pytest.raises(ValueError, match=r"argv|url"):
        load_user_apps(cfg)


def test_build_registry_merges_builtin_and_user(tmp_path: Path) -> None:
    from types import SimpleNamespace

    fake_cfg = SimpleNamespace(apps={"my_md": _user_app_cfg()})
    reg = build_registry(fake_cfg)
    assert "my_md" in reg
    assert "system" in reg
    assert "skim" in reg


def test_user_app_handler_dispatches_via_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured.append(list(argv))
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(apps.subprocess, "run", fake_run)
    cfg = {"toy": _user_app_cfg(argv=["toy", "-f", "{path}"])}
    app = load_user_apps(cfg)["toy"]
    req = OpenRequest(path=Path("/tmp/x.md"), kind="md")
    rc = app.handler(req)
    assert rc == 0
    assert captured == [["toy", "-f", "/tmp/x.md"]]


def test_user_url_app_dispatches_through_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured.append(list(argv))
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(apps.subprocess, "run", fake_run)
    cfg = {
        "deeplink": {
            "display_name": "Deeplink",
            "handles": ["md"],
            "url": "deeplink://{path_pct}?line={line}",
        }
    }
    app = load_user_apps(cfg)["deeplink"]
    req = OpenRequest(path=Path("/tmp/x.md"), kind="md", line=12)
    app.handler(req)
    assert len(captured) == 1
    assert captured[0][0] == "open"
    assert captured[0][1].startswith("deeplink://")
    assert "line=12" in captured[0][1]


# ── ax_trusted caching ───────────────────────────────────────────────────


def test_ax_trusted_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    apps._reset_ax_cache()
    call_count = {"n": 0}

    def fake_probe() -> bool:
        call_count["n"] += 1
        return True

    monkeypatch.setattr(apps, "_probe_ax_trusted", fake_probe)
    assert apps.ax_trusted() is True
    assert apps.ax_trusted() is True
    assert call_count["n"] == 1


def test_ax_trusted_false_triggers_preview_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AX is denied, the Preview handler MUST fall back to a plain
    ``open -a Preview <path>`` and emit exactly one notify side-effect."""
    apps._reset_ax_cache()
    monkeypatch.setattr(apps, "_probe_ax_trusted", lambda: False)

    notifications: list[str] = []
    monkeypatch.setattr(apps, "_emit_notice", lambda msg: notifications.append(msg))

    captured: list[list[str]] = []
    monkeypatch.setattr(
        apps.subprocess,
        "run",
        lambda argv, **kw: captured.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )

    req = OpenRequest(path=Path("/tmp/p.pdf"), kind="pdf", page=5)
    rc = apps.BUILTIN_APPS["preview"].handler(req)
    assert rc == 0
    assert captured == [["open", "-a", "Preview", "/tmp/p.pdf"]]
    assert len(notifications) >= 1
    assert "Accessibility" in notifications[0] or "accessibility" in notifications[0]


# ── Validators ───────────────────────────────────────────────────────────


def test_load_user_apps_rejects_bad_id_chars() -> None:
    with pytest.raises(ValueError, match=r"id|name"):
        load_user_apps({"bad id with spaces": _user_app_cfg()})


def test_load_user_apps_rejects_unknown_handle_kind() -> None:
    with pytest.raises(ValueError, match=r"handle"):
        load_user_apps({"x": _user_app_cfg(handles=["banana"])})


# ── Obsidian handler: never silently swaps apps ────────────────────────


def test_obsidian_handler_with_vault_uses_vault_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        apps.subprocess,
        "run",
        lambda argv, **kw: captured.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )
    req = OpenRequest(
        path=Path("/Users/me/Vault/note.md"),
        kind="md",
        vault="MyVault",
        file_in_vault="note.md",
        heading_path="Findings",
    )
    apps.BUILTIN_APPS["obsidian"].handler(req)
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "open"
    assert argv[1].startswith("obsidian://open?vault=MyVault")
    assert "file=note.md" in argv[1]
    assert "%23Findings" in argv[1]


def test_obsidian_handler_without_vault_uses_path_form_not_system_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug fix regression: when the user explicitly picks Obsidian from
    the "Open with…" menu but no vault is configured, the handler
    MUST still fire Obsidian (via ``?path=<abs>``) rather than silently
    delegating to ``open <path>`` (which would open the macOS default
    for that file kind — often VS Code for .md — and look like a
    silent app-swap to the user)."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        apps.subprocess,
        "run",
        lambda argv, **kw: captured.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )
    req = OpenRequest(
        path=Path("/Users/me/elsewhere/note.md"),
        kind="md",
        vault="",  # no vault
        heading_path="Intro",
    )
    apps.BUILTIN_APPS["obsidian"].handler(req)
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "open"
    # MUST be an obsidian:// URL, NOT a plain ``open <path>``.
    assert argv[1].startswith(
        "obsidian://open?path="
    ), f"obsidian handler with no vault silently swapped apps: {argv}"
    assert "note.md" in argv[1]
    assert "%23Intro" in argv[1]


def test_obsidian_handler_converts_breadcrumb_to_chained_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``heading_path`` is a breadcrumb ("A > B > C") in the chunk;
    Obsidian's anchor syntax wants nested headings separated by ``#``
    ("A#B#C"). Without this rewrite, the anchor reads ``#A > B > C`` —
    Obsidian opens the file but cannot find that heading and lands at
    the top. Regression test for the user-reported "Obsidian opened
    the file but not at the location"."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        apps.subprocess,
        "run",
        lambda argv, **kw: captured.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )
    req = OpenRequest(
        path=Path("/Users/me/Vault/note.md"),
        kind="md",
        vault="MyVault",
        file_in_vault="note.md",
        heading_path="Week 9 > Cyber Kill Chain > Reconnaissance",
    )
    apps.BUILTIN_APPS["obsidian"].handler(req)
    argv = captured[0]
    # Chained-heading anchor (URL-encoded #): Week%209%23Cyber%20Kill%20Chain%23Reconnaissance.
    assert "%23Cyber%20Kill%20Chain%23Reconnaissance" in argv[1], argv[1]
    # The raw " > " breadcrumb separator MUST NOT survive into the URL —
    # spaces around ` > ` would percent-encode to %20%3E%20 and Obsidian
    # would fail to navigate.
    assert "%20%3E%20" not in argv[1], argv[1]


def test_pdf_expert_uses_open_dash_a_not_url_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pdf-expert-7:// URL scheme isn't registered (Info.plist
    only exposes ``pdfexpert://``) and the accepted URL format is
    undocumented. Handler must use ``open -a 'PDF Expert' <path>``
    which always opens the file; page-jump isn't supported but the
    file opens reliably."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        apps.subprocess,
        "run",
        lambda argv, **kw: captured.append(list(argv)) or type("R", (), {"returncode": 0})(),
    )
    req = OpenRequest(path=Path("/tmp/paper.pdf"), kind="pdf", page=7)
    apps.BUILTIN_APPS["pdf_expert"].handler(req)
    assert captured == [["open", "-a", "PDF Expert", "/tmp/paper.pdf"]], captured


def test_heading_path_to_anchor_strips_empty_segments() -> None:
    """Defensive: trailing/leading separators or duplicated ` > ` from
    quirky source files shouldn't yield empty ## sequences in the
    anchor (Obsidian treats `##` as an anchor reset)."""
    assert apps._heading_path_to_anchor("A > B > C") == "A#B#C"
    assert apps._heading_path_to_anchor("  > A >   > B > ") == "A#B"
    assert apps._heading_path_to_anchor("Solo") == "Solo"
    assert apps._heading_path_to_anchor("") == ""
