"""The per-collection 'Rebuild index' action must reach the indexer as a
true rebuild — drop chunks (rebuild) + bypass durable cache (force_fresh)
+ revisit every file (skip_unchanged off) + texturise on. Regression for
a Rebuild that silently ran a plain incremental update.

Drives the real menu action callback against the real app, capturing the
exact kwargs start_indexer receives — the layer where the bug would hide.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
from fnd.index import build_index
from fnd.tui import FNDApp


def _make_cfg(tmp_path: Path) -> tuple[Config, Path]:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "alpha.md").write_text("# alpha\n\nbody\n")
    cfg = Config(
        defaults=Defaults(),
        collections={"papers": CollectionConfig(sources=[SourceConfig(path=root)])},
    )
    index_dir = tmp_path / "index"
    build_index(roots=[root], index_dir=index_dir, collection="papers")
    return cfg, index_dir


@pytest.mark.asyncio
async def test_collection_rebuild_action_passes_rebuild_flags(tmp_path: Path) -> None:
    from fnd.tui.menu import _make_rebuild

    cfg, index_dir = _make_cfg(tmp_path)
    app = FNDApp(index_dir=index_dir, config=cfg)
    captured: list[dict[str, Any]] = []

    async with app.run_test() as pilot:
        await pilot.pause()

        # Capture what the real action chain hands the indexer; don't
        # actually spawn a run.
        def _fake_start(**kwargs: Any) -> bool:
            captured.append(kwargs)
            return True

        app.start_indexer = _fake_start  # type: ignore[assignment]
        _make_rebuild("papers")(app)
        await pilot.pause()

    assert captured, "Rebuild action never reached start_indexer"
    kw = captured[0]
    assert kw.get("rebuild") is True
    assert kw.get("force_fresh") is True
    assert kw.get("skip_unchanged") is False
    assert kw.get("texturise_override") is True
