"""The cloud-placeholder seam: detection is per-platform, never per-path."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from fnd.cloud_files import (
    Materialisation,
    is_placeholder,
    materialisation,
    provider_label,
)


def _fake_stat(**extras: int) -> Callable[..., SimpleNamespace]:
    """A drop-in for the module's ``_stat`` reporting the given extras.

    A plain namespace rather than ``os.stat_result``: the real struct only
    materialises the extras its own platform defines, so a macOS test host
    silently drops ``st_file_attributes`` and the Windows cases would pass
    for the wrong reason.
    """
    result = SimpleNamespace(**extras)

    def _stat(*_a: object, **_kw: object) -> SimpleNamespace:
        return result

    return _stat


def test_missing_file_is_unknown(tmp_path: Path) -> None:
    assert materialisation(tmp_path / "nope.md") is Materialisation.UNKNOWN
    assert is_placeholder(tmp_path / "nope.md") is False


def test_macos_dataless_flag_marks_a_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fnd.cloud_files.sys.platform", "darwin")
    monkeypatch.setattr("fnd.cloud_files._stat", _fake_stat(st_flags=0x40000000))
    assert materialisation(tmp_path / "x.md") is Materialisation.PLACEHOLDER


def test_macos_local_file_is_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fnd.cloud_files.sys.platform", "darwin")
    monkeypatch.setattr("fnd.cloud_files._stat", _fake_stat(st_flags=0))
    assert materialisation(tmp_path / "x.md") is Materialisation.LOCAL


def test_detection_is_independent_of_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An evicted file under ~/Documents is as dataless as one in the
    iCloud Drive folder — Desktop & Documents sync keeps ordinary paths."""
    monkeypatch.setattr("fnd.cloud_files.sys.platform", "darwin")
    monkeypatch.setattr("fnd.cloud_files._stat", _fake_stat(st_flags=0x40000000))
    for where in (
        Path.home() / "Documents" / "Uni" / "notes.md",
        Path.home() / "Library" / "Mobile Documents" / "vault" / "notes.md",
        Path("/Volumes/External/notes.md"),
    ):
        assert is_placeholder(where) is True, where


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        (0x00001000, Materialisation.PLACEHOLDER),  # OFFLINE
        (0x00040000, Materialisation.PLACEHOLDER),  # RECALL_ON_OPEN
        (0x00400000, Materialisation.PLACEHOLDER),  # RECALL_ON_DATA_ACCESS
        (0x00000020, Materialisation.LOCAL),  # ARCHIVE only
    ],
)
def test_windows_placeholder_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attrs: int, expected: Materialisation
) -> None:
    monkeypatch.setattr("fnd.cloud_files.sys.platform", "win32")
    monkeypatch.setattr("fnd.cloud_files._stat", _fake_stat(st_file_attributes=attrs))
    assert materialisation(tmp_path / "x.md") is expected


def test_linux_reports_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No common marker on Linux — say so rather than guessing, and treat
    it as local so nothing is skipped for a condition we can't detect."""
    monkeypatch.setattr("fnd.cloud_files.sys.platform", "linux")
    monkeypatch.setattr("fnd.cloud_files._stat", _fake_stat())
    p = tmp_path / "x.md"
    assert materialisation(p) is Materialisation.UNKNOWN
    assert is_placeholder(p) is False


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Users/x/Library/Mobile Documents/iCloud~md~obsidian/Vault/a.md", "iCloud Drive"),
        ("/Users/x/Library/CloudStorage/OneDrive-Personal/a.md", "OneDrive"),
        ("/Users/x/Library/CloudStorage/Dropbox/a.md", "Dropbox"),
        ("C:\\Users\\x\\OneDrive - Contoso\\a.md", "OneDrive"),
    ],
)
def test_provider_label_recognises_known_mounts(path: str, expected: str) -> None:
    assert provider_label(Path(path)) == expected


def test_provider_label_falls_back_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fnd.cloud_files.platform.system", lambda: "Linux")
    assert provider_label(Path("/srv/share/a.md")) == "cloud storage"
