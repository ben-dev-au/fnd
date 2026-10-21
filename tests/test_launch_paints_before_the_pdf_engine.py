"""The first frame never waits on the PDF engine: pymupdf loads only after it, if at all."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from fnd.config import Config
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


class _Tripwire(types.ModuleType):
    """Stands in for ``fnd.extract.pdf`` and records each name imported from it."""

    def __init__(self) -> None:
        super().__init__("fnd.extract.pdf")
        self.reads: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.reads.append(name)
        raise AttributeError(name)


def _promote(cache_root: Path, index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    tripwire = _Tripwire()
    monkeypatch.setitem(sys.modules, "fnd.extract.pdf", tripwire)
    FNDApp(index_dir=index_dir, config=Config())._promote_texture_cache_keys()  # pyright: ignore[reportPrivateUsage]
    return tripwire.reads


def test_a_promoted_cache_never_loads_the_pdf_engine(
    isolated_pdf_structure_cache: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_pdf_structure_cache.mkdir(parents=True)
    (isolated_pdf_structure_cache / ".keys-migrated").write_text("tex-v1", encoding="utf-8")
    assert _promote(isolated_pdf_structure_cache, tmp_index_dir, monkeypatch) == []


def test_an_unpromoted_cache_loads_it(
    isolated_pdf_structure_cache: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the tripwire sees the import when there is a promotion to run."""
    isolated_pdf_structure_cache.mkdir(parents=True)
    assert _promote(isolated_pdf_structure_cache, tmp_index_dir, monkeypatch)


@pytest.mark.asyncio
async def test_the_promotion_waits_for_on_mount_to_return(
    tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mounted: list[bool] = []
    seen: list[bool] = []
    on_mount = FNDApp.on_mount

    def recording_on_mount(self: FNDApp) -> None:
        on_mount(self)
        mounted.append(True)

    monkeypatch.setattr(FNDApp, "on_mount", recording_on_mount)
    monkeypatch.setattr(
        FNDApp, "_promote_texture_cache_keys", lambda _self: seen.append(bool(mounted))
    )
    app = FNDApp(index_dir=tmp_index_dir, config=Config())
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: bool(seen), timeout=10.0, message="never promoted")
    assert seen == [True]
