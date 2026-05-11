"""Phase 3 (Settings UX redesign) — Add Collection wizard tests."""

from __future__ import annotations


def test_excludes_presets_exposed() -> None:
    """Spec: Wizard › Excludes — preset patterns, with safe defaults."""
    from acorn.config import EXCLUDES_PRESETS

    assert "hidden" in EXCLUDES_PRESETS
    hidden = EXCLUDES_PRESETS["hidden"]
    assert hidden["label"] == "Hidden / system"
    assert any(".git" in g for g in hidden["globs"])
    assert hidden["default"] is True  # pre-ticked
    assert "node_modules" in EXCLUDES_PRESETS
    assert EXCLUDES_PRESETS["node_modules"]["default"] is False
