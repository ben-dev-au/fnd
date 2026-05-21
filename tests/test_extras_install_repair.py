"""Repair of broken dist-info dirs before extras install.

macOS Finder / iCloud sync leaves files with a `` 2`` suffix when
there's a conflict. The original ``METADATA`` disappears,
``METADATA 2`` survives, and ``uv pip install`` then refuses to
operate on the venv with exit 1. fnd sweeps these stale dist-info
directories before running pip install."""

from __future__ import annotations

from pathlib import Path

from fnd.tui.extras_install_progress import repair_orphan_dist_info


def test_removes_dist_info_missing_metadata(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    broken = site_packages / "tabulate-0.10.0.dist-info"
    broken.mkdir()
    # Files survived the conflict but METADATA didn't.
    (broken / "METADATA 2").write_text("# stale")
    (broken / "RECORD 2").write_text("# stale")

    cleaned = repair_orphan_dist_info(site_packages)
    assert "tabulate-0.10.0.dist-info" in cleaned
    assert not broken.exists()


def test_preserves_dist_info_with_metadata(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    intact = site_packages / "ok-1.0.0.dist-info"
    intact.mkdir()
    (intact / "METADATA").write_text("Name: ok\nVersion: 1.0.0\n")

    cleaned = repair_orphan_dist_info(site_packages)
    assert cleaned == []
    assert intact.exists()
    assert (intact / "METADATA").exists()


def test_handles_missing_site_packages(tmp_path: Path) -> None:
    """No site-packages dir → no-op, no exception."""
    assert repair_orphan_dist_info(tmp_path / "nonexistent") == []


def test_handles_mixed_state(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()

    broken = site_packages / "broken-1.0.dist-info"
    broken.mkdir()
    (broken / "WHEEL 2").write_text("# stale")

    intact = site_packages / "intact-2.0.dist-info"
    intact.mkdir()
    (intact / "METADATA").write_text("Name: intact\n")

    cleaned = repair_orphan_dist_info(site_packages)
    assert cleaned == ["broken-1.0.dist-info"]
    assert not broken.exists()
    assert intact.exists()
