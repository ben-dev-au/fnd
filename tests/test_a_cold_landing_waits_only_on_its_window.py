"""A landing waits for the chunks the mount puts above its target before the
reveal, and no others: waiting on the fixed seven-chunk cap spent the whole
retry budget (0.9s on a real file) on chunks that only arrive after the reveal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until

TERM = "scaffold"


def _corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Four short sections above the match: a screenful needs fewer than seven."""
    docs = tmp_path / "docs"
    docs.mkdir()
    filler = "".join(f"Filler line {i} with a few words.\n\n" for i in range(6))
    parts = ["# Cheatsheet\n\n" + filler]
    parts += [f"## Topic {i}\n\n" + filler for i in range(3)]
    parts.append(f"## Terminal\n\n### Scaffold a controller\n\nRun the {TERM} step.\n\n" + filler)
    parts += [f"## Later {i}\n\n" + filler for i in range(60)]
    (docs / "sheet.md").write_text("".join(parts), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_cold_landing_commits_before_its_retry_budget_runs_out(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd.tui.preview_scroll import StructuralScrollStrategy

    attempts: list[int] = []
    original = StructuralScrollStrategy._do_scroll_to_chunk

    def counted(self: Any, focus_chunk_seq: int, retries: int = 30, *a: Any, **kw: Any) -> None:
        attempts.append(retries)
        original(self, focus_chunk_seq, retries, *a, **kw)

    monkeypatch.setattr(StructuralScrollStrategy, "_do_scroll_to_chunk", counted)
    app = FNDApp(index_dir=_corpus(tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(116, 45)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(attempts) and not app._preview_scroll.is_settling,
            timeout=20,
            message="the landing never committed",
        )
        assert min(attempts) > 0, f"spent the whole budget: {len(attempts)} attempts"
