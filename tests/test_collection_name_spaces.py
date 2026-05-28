"""Round-trip tests for collection names that contain spaces and other
non-bare TOML characters. Covers the relaxation that landed alongside
the SSD-reindex freeze fix: validate_collection_name now allows display
strings like 'Soft Eng Textbooks' as long as they're safe for TOML keys,
the per-collection state file path, and the c:"…" DSL shorthand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import (
    SourceConfig,
    load,
    validate_collection_name,
    write_collection_source,
)
from fnd.index_runner import state_file_for
from fnd.query_dsl import preprocess


def test_spaced_name_round_trips_through_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The writer auto-discovers ~/Library/Application Support/fnd via
    # default_config_path, so steer it at tmp_path with an explicit arg.
    cfg = tmp_path / "config.toml"
    name = "Soft Eng Textbooks"
    src = SourceConfig(path=tmp_path, includes=["**/*.md"])
    write_collection_source(config_path=cfg, collection_name=name, source=src)

    written = cfg.read_text(encoding="utf-8")
    # tomlkit auto-quotes the key because of the spaces — the unquoted
    # form would be a TOML parse error.
    assert f'[[collections."{name}".sources]]' in written

    # And read-back through the Config model preserves the name verbatim.
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg)
    loaded = load()
    assert name in loaded.collections


def test_state_file_path_handles_spaced_name() -> None:
    """The state file is per-collection; spaces in the name flow through
    to the filename. macOS / Linux accept spaces in filenames; the only
    risk would be path-component injection (``/``), which the validator
    blocks."""
    p = state_file_for("Soft Eng Textbooks")
    assert p.name == "Soft Eng Textbooks.state.toml"


def test_spaced_name_validates_and_dsl_round_trips() -> None:
    name = "Soft Eng Textbooks"
    validate_collection_name(name)  # would raise if not accepted
    out = preprocess(f'c:"{name}" tdd')
    assert out == f'collection:"{name}" tdd'
