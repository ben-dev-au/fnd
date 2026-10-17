"""Config v4 drops the section score cut-off: every matching section is shown."""

from __future__ import annotations

from typing import Any

from fnd.config import Config
from fnd.config_migrations import CONFIG_VERSION, migrate


def test_a_set_cut_off_is_dropped_and_the_config_loads() -> None:
    """A user who set it is not locked out by the key's removal."""
    raw: dict[str, Any] = {"config_version": 3, "defaults": {"sections_score_threshold": 0.3}}
    version, applied = migrate(raw)
    assert version == CONFIG_VERSION
    assert "sections_score_threshold" not in raw["defaults"]
    assert any("cut-off" in note for note in applied)
    Config.model_validate(raw)
