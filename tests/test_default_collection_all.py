"""``defaults.collection`` = "all", and ``-c all`` as its one-off spelling.

A fresh profile should search everything rather than a collection named
``default`` that most users never create. The setting seeds scope only when
nothing has been remembered — once the sidebar has saved a selection, that
wins — so ``-c all`` exists as the quick per-launch override.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import (
    ALL_COLLECTIONS,
    CollectionConfig,
    Config,
    Defaults,
    InvalidCollectionNameError,
    SourceConfig,
    is_all_collections,
    validate_collection_name,
)


def test_fresh_config_defaults_to_all_collections() -> None:
    assert Defaults().collection == ALL_COLLECTIONS


@pytest.mark.parametrize("value", ["all", "All", "ALL", "  all  "])
def test_all_is_matched_case_and_space_insensitively(value: str) -> None:
    assert is_all_collections(value) is True


@pytest.mark.parametrize("value", ["", None, "papers", "allsorts"])
def test_other_values_are_not_the_pseudo_name(value: str | None) -> None:
    assert is_all_collections(value) is False


def test_a_real_collection_named_all_wins() -> None:
    """Configs written before the name was reserved keep working — their
    own collection resolves, rather than silently widening to everything."""
    assert is_all_collections("all", known={"all", "papers"}) is False


def test_creating_a_collection_named_all_is_refused() -> None:
    with pytest.raises(InvalidCollectionNameError, match="reserved"):
        validate_collection_name("all")
    with pytest.raises(InvalidCollectionNameError, match="reserved"):
        validate_collection_name("All")


def _config(tmp_path: Path, names: list[str], *, default: str) -> Config:
    return Config(
        defaults=Defaults(collection=default),
        collections={n: CollectionConfig(sources=[SourceConfig(path=tmp_path / n)]) for n in names},
    )


class _StubApp:
    def __init__(self, cfg: Config) -> None:
        self._config = cfg


def _scope(cfg: Config, *, collection: str | None, state_saved: bool, saved_names: list[str]):
    from unittest.mock import patch

    from fnd.state import UiState
    from fnd.tui.scope_panel import ScopeController

    state = UiState(collections=list(saved_names), saved=state_saved)
    with patch("fnd.state.load", return_value=state):
        return ScopeController(_StubApp(cfg), collection=collection)  # type: ignore[arg-type]


def test_first_launch_selects_every_collection(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["alpha", "beta"], default=ALL_COLLECTIONS)
    scope = _scope(cfg, collection=None, state_saved=False, saved_names=[])
    assert sorted(scope.collections) == ["alpha", "beta"]


def test_first_launch_honours_a_named_default(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["alpha", "beta"], default="beta")
    scope = _scope(cfg, collection=None, state_saved=False, saved_names=[])
    assert scope.collections == ["beta"]


def test_saved_empty_scope_is_not_overwritten_by_the_default(tmp_path: Path) -> None:
    """A user who deliberately deselected everything must not have the
    default silently re-tick all their collections on next launch."""
    cfg = _config(tmp_path, ["alpha", "beta"], default=ALL_COLLECTIONS)
    scope = _scope(cfg, collection=None, state_saved=True, saved_names=[])
    assert scope.collections == []


def test_saved_scope_wins_over_the_default(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["alpha", "beta"], default=ALL_COLLECTIONS)
    scope = _scope(cfg, collection=None, state_saved=True, saved_names=["alpha"])
    assert scope.collections == ["alpha"]


@pytest.mark.parametrize("flag", ["all", "All"])
def test_dash_c_all_scopes_every_collection(tmp_path: Path, flag: str) -> None:
    cfg = _config(tmp_path, ["alpha", "beta"], default="alpha")
    scope = _scope(cfg, collection=flag, state_saved=True, saved_names=["alpha"])
    assert sorted(scope.collections) == ["alpha", "beta"]


def test_dash_c_name_still_scopes_that_one(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["alpha", "beta"], default=ALL_COLLECTIONS)
    scope = _scope(cfg, collection="beta", state_saved=True, saved_names=["alpha"])
    assert scope.collections == ["beta"]
