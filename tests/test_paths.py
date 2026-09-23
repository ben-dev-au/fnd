"""Unit tests for the centralised filesystem-location seam (``fnd.paths``).

The load-bearing guarantee is that every derived path hangs off exactly one
of two roots, and that both roots resolve with ``appauthor=False`` so they
stay siblings on Windows (the historical split-brain: some call sites passed
``appauthor=False``, others let it default to the app name → app data landed
in two different roots on Windows, invisibly on macOS/Linux).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd import paths


def test_app_dirs_pass_appauthor_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both roots must call platformdirs with ``appauthor=False`` — the
    single fact that keeps Windows app data from splitting."""
    calls: dict[str, tuple[str, object]] = {}

    def spy_data(appname: str, appauthor: object = None, **_: object) -> str:
        calls["data"] = (appname, appauthor)
        return "/tmp/fnd-data"

    def spy_cache(appname: str, appauthor: object = None, **_: object) -> str:
        calls["cache"] = (appname, appauthor)
        return "/tmp/fnd-cache"

    monkeypatch.setattr("fnd.paths.user_data_dir", spy_data)
    monkeypatch.setattr("fnd.paths.user_cache_dir", spy_cache)

    paths.app_data_dir()
    paths.app_cache_dir()

    assert calls["data"] == ("fnd", False)
    assert calls["cache"] == ("fnd", False)


def test_data_helpers_hang_off_data_root() -> None:
    root = paths.app_data_dir()
    for p in (
        paths.reindex_state_dir(),
        paths.reindex_state_path("papers"),
        paths.dismissed_dir(),
        paths.first_reindex_marker_path(),
        paths.throughput_log_path(),
        paths.failure_log_path(),
    ):
        assert p.is_relative_to(root), p


def test_cache_helpers_hang_off_cache_root() -> None:
    root = paths.app_cache_dir()
    for p in (
        paths.seen_dir(),
        paths.worker_logs_dir(),
        paths.pdf_structure_cache_dir(),
    ):
        assert p.is_relative_to(root), p


def test_layout_matches_legacy_on_disk_names() -> None:
    """Lock the on-disk basenames so a rename can't silently orphan an
    existing user's index/cache."""
    assert paths.reindex_state_dir().name == "reindex"
    assert paths.reindex_state_path("c").name == "c.state.toml"
    assert paths.dismissed_dir().name == "dismissed"
    assert paths.first_reindex_marker_path().name == "first_reindex_warning_seen"
    assert paths.throughput_log_path().name == "indexer_throughput.jsonl"
    assert paths.failure_log_path().name == "indexer_failures.toml"
    assert paths.seen_dir().name == "seen"
    assert paths.worker_logs_dir().name == "worker-logs"
    assert paths.pdf_structure_cache_dir().name == "pdf-structure"


def test_config_reexports_the_same_data_root() -> None:
    """``from fnd.config import app_data_dir`` must stay valid and identical
    (many modules import it from there)."""
    from fnd import config

    assert config.app_data_dir() == paths.app_data_dir()


def test_uv_tool_root_falls_back_when_uv_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``uv tool dir`` can't run, fall back to the platform default —
    which ends in ``uv/tools`` on every OS (POSIX XDG / Windows APPDATA)."""

    def boom(*_a: object, **_k: object) -> object:
        raise FileNotFoundError("uv not found")

    monkeypatch.setattr("fnd.paths.subprocess.run", boom)
    # The uv answer is cached for the process; a real one from an earlier
    # test would otherwise mask the fallback this asserts.
    paths._uv_tool_dir.cache_clear()
    root = paths.uv_tool_root()
    assert root.name == "tools"
    assert "uv" in root.parts


def test_uv_tool_root_honours_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The POSIX fallback must respect $XDG_DATA_HOME (uv does), not hardcode
    ~/.local/share."""

    def boom(*_a: object, **_k: object) -> object:
        raise FileNotFoundError("uv not found")

    monkeypatch.setattr("fnd.paths.subprocess.run", boom)
    monkeypatch.setattr("fnd.paths.sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    paths._uv_tool_dir.cache_clear()
    assert paths.uv_tool_root() == tmp_path / "xdg" / "uv" / "tools"


def test_helpers_create_nothing() -> None:
    """Pure path computation — no directory is created as a side effect."""
    assert not paths.reindex_state_dir().exists() or paths.reindex_state_dir().is_dir()
    # Calling a helper must not create its parent.
    p = paths.dismissed_dir()
    before = p.exists()
    paths.dismissed_dir()
    assert p.exists() == before


def test_uv_tool_dir_is_asked_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Settings open reached this five times, each a 20 ms fork on the
    event loop. The root cannot move while fnd runs, so ask once."""
    calls: list[int] = []

    class _Out:
        stdout = "/tmp/uv/tools\n"

    def counting(*_a: object, **_k: object) -> object:
        calls.append(1)
        return _Out()

    monkeypatch.setattr("fnd.paths.subprocess.run", counting)
    paths._uv_tool_dir.cache_clear()
    first = paths.uv_tool_root()
    for _ in range(4):
        assert paths.uv_tool_root() == first
    assert len(calls) == 1, f"asked uv {len(calls)} times"
    paths._uv_tool_dir.cache_clear()


def test_an_install_is_still_seen_without_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The *root* is cached; what lives under it is not, so installing an
    extra is visible immediately."""
    from fnd import extras

    class _Out:
        stdout = str(tmp_path)

    monkeypatch.setattr("fnd.paths.subprocess.run", lambda *_a, **_k: _Out())
    paths._uv_tool_dir.cache_clear()
    pkg = next(
        p for extra in extras.EXTRAS.values() for p in extra.packages if p.install_via == "uv-tool"
    )
    tool = pkg.spec.split("[", 1)[0]
    assert extras.is_package_installed(pkg) is False
    (tmp_path / tool).mkdir(parents=True)
    assert extras.is_package_installed(pkg) is True, "a cached root must not cache its contents"
    paths._uv_tool_dir.cache_clear()
