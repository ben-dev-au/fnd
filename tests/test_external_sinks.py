"""Text leaving fnd for a process, a URL, a file name or a fixed-width cell is encoded once, by its sink."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from rich.cells import cell_len

from fnd.apps import OpenRequest, _render_argv, _render_url  # pyright: ignore[reportPrivateUsage]

# ── $EDITOR (finding 7) ──────────────────────────────────────────────


def test_visual_wins_and_a_flag_stays_a_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from fnd.launcher import editor_argv

    monkeypatch.setenv("VISUAL", "code -w")
    monkeypatch.setenv("EDITOR", "nano")
    target = Path("/x/c.toml")
    assert editor_argv(target) == ["code", "-w", str(target)]


def test_a_missing_editor_is_a_return_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from fnd.launcher import LAUNCH_FAILED, edit

    monkeypatch.setenv("VISUAL", "fnd-no-such-editor -w")
    assert edit(Path("/x/c.toml")) == LAUNCH_FAILED


def test_config_edit_names_an_editor_it_cannot_start(
    isolated_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from fnd.cli import app

    monkeypatch.setenv("VISUAL", "fnd-no-such-editor -w")
    monkeypatch.setenv("EDITOR", "fnd-no-such-editor -w")
    result = CliRunner().invoke(app, ["config", "edit"])
    assert result.exit_code != 0
    assert "fnd-no-such-editor" in result.output
    assert not isinstance(result.exception, FileNotFoundError)


# ── spawns (finding 16) ──────────────────────────────────────────────

# Modules that run a process for its output or its exit status, not to open a
# file for the user; each has its own reason and is not a launcher concern.
_SUBPROCESS_ALLOWED = {
    "launcher.py",
    "paths.py",
    "extras.py",
    "tui/clipboard.py",
    "tui/extras_install_progress.py",
    "extract/_docling_daemon.py",
}


def test_every_spawn_goes_through_the_launcher() -> None:
    root = Path(__file__).resolve().parent.parent / "fnd"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in _SUBPROCESS_ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            spawns = (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "subprocess"
                and node.attr in ("run", "call", "Popen", "check_call", "check_output")
            )
            imported = isinstance(node, ast.ImportFrom) and node.module == "subprocess"
            spawns_async = isinstance(node, ast.Attribute) and node.attr.startswith(
                "create_subprocess_"
            )
            if spawns or imported or spawns_async:
                offenders.append(f"{rel}:{getattr(node, 'lineno', 0)}")
    assert not offenders, offenders


def test_an_unquoted_editor_path_holding_a_space_is_one_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd.launcher import editor_argv

    # Windows' which() matches only a name with an executable extension.
    program = tmp_path / ("My Editor.exe" if sys.platform == "win32" else "My Editor")
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o755)
    monkeypatch.setenv("VISUAL", str(program))
    target = Path("/x/c.toml")
    assert editor_argv(target) == [str(program), str(target)]


def test_a_quoted_windows_editor_loses_its_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    from fnd import launcher

    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher, "which", lambda _name: None)
    monkeypatch.setenv("VISUAL", '"C:\\Program Files\\VS Code\\code.cmd" --wait')
    assert launcher.editor_argv(Path("c.toml")) == [
        "C:\\Program Files\\VS Code\\code.cmd",
        "--wait",
        "c.toml",
    ]


@pytest.mark.asyncio
async def test_the_tui_says_when_the_editor_cannot_start(
    tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    from fnd import launcher
    from fnd.tui import FNDApp

    monkeypatch.setattr(launcher, "edit", lambda _p: launcher.LAUNCH_FAILED)
    app = FNDApp(index_dir=tmp_index_dir)
    said: list[str] = []
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
        monkeypatch.setattr(app, "notify", lambda msg, **_kw: said.append(str(msg)))
        opened = app._edit_in_editor(Path("/x/c.toml"))  # pyright: ignore[reportPrivateUsage]
    assert opened is False
    assert any("Could not start the editor" in m for m in said), said


def test_o_says_when_the_app_for_a_file_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from fnd import opener
    from fnd.launcher import LAUNCH_FAILED
    from fnd.tui.app import FNDApp

    doc = tmp_path / "a.md"
    doc.write_text("x", encoding="utf-8")
    hit = SimpleNamespace(path=str(doc), kind="md", page=0)
    said: list[str] = []
    stub = SimpleNamespace(
        query_one=lambda *_a: SimpleNamespace(cursor_node=object()),
        _results=SimpleNamespace(target_for_node=lambda _n: (None, hit)),
        _refuse_if_missing=lambda _p: False,
        _search=SimpleNamespace(current_query="x"),
        _source_for_hit=lambda _h: None,
        notify=lambda msg, **_kw: said.append(str(msg)),
    )
    monkeypatch.setattr(opener, "open_smart", lambda **_kw: LAUNCH_FAILED)
    FNDApp.action_open_at_locator(stub)  # pyright: ignore[reportArgumentType]
    assert any("could not be started" in m for m in said), said


# ── templates (findings 13, 17) ──────────────────────────────────────


def test_a_url_placeholder_is_always_percent_encoded() -> None:
    req = OpenRequest(path=Path("/n/a.md"), kind="md", heading_path="A&evil=1 #x")
    assert _render_url("x://open?h={heading}", req) == "x://open?h=A%26evil%3D1%20%23x"


def test_a_url_path_keeps_its_slashes() -> None:
    from urllib.parse import quote

    path = Path("/n/my a.md")
    req = OpenRequest(path=path, kind="md", line=3)
    want = f"vscode://file{quote(str(path), safe='/:')}:3"
    assert _render_url("vscode://file{path}:{line}", req) == want


def test_a_path_ending_in_a_colon_is_opened_as_written() -> None:
    path = Path("/notes/Meeting 10:")
    req = OpenRequest(path=path, kind="md")
    assert _render_argv(["ed", "{path}"], req) == ["ed", str(path)]


def test_an_empty_locator_still_collapses() -> None:
    path = Path("/n/a.md")
    req = OpenRequest(path=path, kind="md")
    assert _render_argv(["code", "-g", "{path}:{line}:1"], req) == ["code", str(path)]


# ── file names (finding 14) ──────────────────────────────────────────


@pytest.mark.parametrize("name", ["notes", "Soft Eng Textbooks", "Notes v1.2", "Études"])
def test_a_plain_name_is_its_own_file_name(name: str) -> None:
    from fnd.paths import safe_filename

    assert safe_filename(name) == name


@pytest.mark.parametrize("name", ["a:b", "a|b", "CON", "x.", "x ", "..", "a*b", "a?b"])
def test_a_name_no_filesystem_takes_becomes_one_that_every_one_does(name: str) -> None:
    from fnd.paths import safe_filename

    out = safe_filename(name)
    assert out != name
    assert not set(out) & set('<>:"/\\|?*')
    assert not out.endswith((".", " "))
    assert out.split(".")[0].upper() not in {"CON", "PRN", "AUX", "NUL"}


def test_two_escaped_names_never_share_a_file() -> None:
    from fnd.paths import safe_filename

    assert safe_filename("a:b") != safe_filename("a|b")


def test_the_state_file_takes_the_safe_name() -> None:
    from fnd.index_runner import state_file_for

    assert ":" not in state_file_for("a:b").name


def test_a_state_saved_under_the_raw_name_moves_to_the_safe_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Else a finished run clears only the new file and the old one resumes forever."""
    from fnd import index_runner

    monkeypatch.setattr(index_runner, "state_dir", lambda: tmp_path)
    # "x." and not "a:b": on NTFS a colon names a stream, not a file.
    (tmp_path / "x..state.toml").write_text("x = 1\n", encoding="utf-8")
    path = index_runner.state_file_for("x.")
    assert path.read_text(encoding="utf-8") == "x = 1\n"
    assert not (tmp_path / "x..state.toml").exists()


# ── diagnostic logs (finding 15) ─────────────────────────────────────


def test_no_diagnostic_log_lives_in_the_shared_temp_dir() -> None:
    root = Path(__file__).resolve().parent.parent / "fnd"
    offenders = [
        f"{p.relative_to(root)}"
        for p in root.rglob("*.py")
        if "gettempdir" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


# ── cell width (finding 36) ──────────────────────────────────────────

WIDE = (
    "\N{CJK UNIFIED IDEOGRAPH-65E5}\N{CJK UNIFIED IDEOGRAPH-672C}\N{CJK UNIFIED IDEOGRAPH-8A9E}"
    * 10
)


@pytest.mark.parametrize("keep", ["start", "end"])
@pytest.mark.parametrize("cells", [1, 2, 5, 9, 59])
def test_fit_never_exceeds_its_cells(keep: str, cells: int) -> None:
    from fnd.display_text import fit

    out = fit(WIDE, cells, keep=keep)  # pyright: ignore[reportArgumentType]
    assert cell_len(out) <= cells
    assert out.endswith("\N{HORIZONTAL ELLIPSIS}") or out.startswith("\N{HORIZONTAL ELLIPSIS}")


EMOJI = "\N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16}" * 10


@pytest.mark.parametrize("keep", ["start", "end"])
def test_fit_counts_an_emoji_as_one_two_cell_unit(keep: str) -> None:
    from fnd.display_text import fit

    out = fit(EMOJI, 6, keep=keep)  # pyright: ignore[reportArgumentType]
    assert cell_len(out) <= 6


def test_a_wide_settings_value_fits_its_budget() -> None:
    from fnd.tui.settings_screen import (
        _truncate_segments_to_fit,  # pyright: ignore[reportPrivateUsage]
    )

    out = _truncate_segments_to_fit([(WIDE[:16], "bold"), (" ▸", "")], budget=20)
    assert sum(cell_len(t) for t, _ in out) <= 20


def test_a_cut_snippet_ends_at_a_word_not_a_space() -> None:
    from fnd.tui.results_labels import _shorten  # pyright: ignore[reportPrivateUsage]

    assert _shorten("Intro to the topic", 7) == "Intro\N{HORIZONTAL ELLIPSIS}"


def test_fit_leaves_text_that_fits() -> None:
    from fnd.display_text import fit

    assert fit(WIDE, 60) == WIDE


def test_a_wide_file_name_fits_its_results_row() -> None:
    from fnd.tui.results_labels import (
        _elide_middle_keep_suffix,  # pyright: ignore[reportPrivateUsage]
    )

    out = _elide_middle_keep_suffix(f"{WIDE}.pdf", 20)
    assert cell_len(out) <= 20
    assert out.endswith(".pdf")


def test_a_wide_file_name_fits_the_indexer_line() -> None:
    from fnd.tui.indexer_modal import _short_name  # pyright: ignore[reportPrivateUsage]

    assert cell_len(_short_name(f"/x/{WIDE}{WIDE}{WIDE}.pdf")) <= 68
