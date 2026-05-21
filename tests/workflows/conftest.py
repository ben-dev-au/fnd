"""Shared fixtures for end-to-end workflow tests.

A workflow test typically wants:

- An ``FNDApp`` mounted in pilot mode with a realistic multi-collection
  config so menu drilling exercises real provider code.
- Isolated state files / cache dirs so tests don't fight each other.
- A helper to walk the settings menu and find a row by id.
- A helper to wait for an event-loop condition (modal dismissed,
  task done, attribute set).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def mini_corpus(tmp_path: Path) -> Path:
    """A tiny on-disk corpus that walks fast in indexer tests."""
    root = tmp_path / "corpus"
    root.mkdir()
    for name in ("alpha", "beta", "gamma"):
        (root / f"{name}.md").write_text(f"# {name}\n\nbody\n")
    return root


@pytest.fixture
def cfg_three(mini_corpus: Path, tmp_path: Path) -> Config:
    """Three named collections, all pointing at the same mini corpus."""
    return Config(
        defaults=Defaults(),
        collections={
            name: CollectionConfig(sources=[SourceConfig(path=mini_corpus)])
            for name in ("alpha", "beta", "gamma")
        },
    )


@pytest.fixture
def cfg_one(mini_corpus: Path) -> Config:
    """One collection — minimal for single-collection workflows."""
    return Config(
        defaults=Defaults(),
        collections={"default": CollectionConfig(sources=[SourceConfig(path=mini_corpus)])},
    )


@pytest.fixture
def built_index(mini_corpus: Path, tmp_path: Path) -> Path:
    """A Tantivy index pre-populated from the mini corpus."""
    index_dir = tmp_path / "index"
    build_index(roots=[mini_corpus], index_dir=index_dir, collection="alpha")
    return index_dir


@pytest.fixture
def app_factory(built_index: Path) -> Callable[[Config], FNDApp]:
    """Build an FNDApp with the given config sharing the prepared index."""

    def _make(cfg: Config) -> FNDApp:
        return FNDApp(index_dir=built_index, config=cfg)

    return _make


async def wait_until(
    pilot: Any, predicate: Callable[[], bool], *, timeout: float = 3.0, ticks: int = 60
) -> bool:
    """Pump the event loop until ``predicate()`` is True or we exhaust
    the budget. Returns True on success.

    pilot.pause() drains pending work; multiple iterations let
    ``call_later`` continuations fire."""
    for _ in range(ticks):
        if predicate():
            return True
        await pilot.pause()
        await asyncio.sleep(0)
    return predicate()
