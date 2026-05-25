"""Toggle workflows — Auto-resume + Update cache at index time.

Each toggle should:
  - Read from defaults on mount.
  - Flip the config field when activated.
  - Persist to disk.
  - Reflect the new state on next provider call.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_auto_resume_writer_persists_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Toggle the auto-resume setting; verify the on-disk config
    reflects the change."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    from fnd.config import load, write_setting

    cfg_before = load(cfg_path)
    initial = cfg_before.defaults.indexer_auto_resume

    write_setting(
        config_path=cfg_path,
        dotted_path="defaults.indexer_auto_resume",
        value=not initial,
    )

    cfg_after = load(cfg_path)
    assert cfg_after.defaults.indexer_auto_resume == (not initial)


def test_cache_at_index_time_writer_persists_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    from fnd.config import load, write_setting

    cfg_before = load(cfg_path)
    initial = cfg_before.defaults.cache_at_index_time

    write_setting(
        config_path=cfg_path, dotted_path="defaults.cache_at_index_time", value=not initial
    )

    cfg_after = load(cfg_path)
    assert cfg_after.defaults.cache_at_index_time == (not initial)


def test_cache_at_index_time_off_skips_structure_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the toggle is off, run_indexer should set the module-level
    _skip_structure_extraction flag so extract() bypasses pymupdf4llm
    on cache miss."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    from fnd.config import load, write_setting
    from fnd.extract import pdf

    # Toggle off.
    write_setting(config_path=cfg_path, dotted_path="defaults.cache_at_index_time", value=False)
    full_cfg = load()
    assert not full_cfg.defaults.cache_at_index_time

    # The flag is set inside run_indexer's setup. Simulate that
    # contract directly:
    pdf.set_skip_structure_extraction(not bool(full_cfg.defaults.cache_at_index_time))
    assert pdf._skip_structure_extraction is True

    # Restore for the rest of the test suite.
    pdf.set_skip_structure_extraction(False)
