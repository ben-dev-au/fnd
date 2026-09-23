"""The Files in index row "Refreshes when an Update index run finishes".

The row's trailing value comes from the lazy-trailing cache under
`indexing.files_in_index`, whose 30-second TTL holds the pre-run count unless
`refresh_items` invalidates that key along with the others.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.tui import FNDApp
from fnd.tui.menu import SECTION_INDEXING
from fnd.tui.settings_screen import SettingsScreen, open_settings_section
from tests._pilot_wait import settings_ready


@pytest.mark.asyncio
async def test_the_row_is_recomputed_when_a_run_finishes(tmp_index_dir: Path) -> None:
    from fnd.tui import lazy_trailing

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)

        # Stand a known value in the cache, as a completed run's own count
        # would be standing there.
        lazy_trailing._CACHE["indexing.files_in_index"] = ("PRERUN-COUNT", 1e18)  # type: ignore[attr-defined]
        screen.refresh_items()
        for _ in range(6):
            await pilot.pause()
        held = lazy_trailing._CACHE.get("indexing.files_in_index")  # type: ignore[attr-defined]

    assert held is None or held[0] != "PRERUN-COUNT", "the run's own count outlived the run"


@pytest.mark.asyncio
async def test_no_cached_row_survives_a_repaint(tmp_index_dir: Path) -> None:
    """Behavioural, not a source scrape: seed every cached key and repaint.

    The previous form read string literals out of `refresh_items` and was
    scoped to `indexing.`, which exempted the two keys it existed to protect.
    Asking the cache what survived cannot be scoped wrong, and it keeps working
    however the invalidation is written.
    """
    import re

    from fnd.tui import lazy_trailing

    menu_src = Path("fnd/tui/menu.py").read_text(encoding="utf-8")
    scheduled = sorted(set(re.findall(r'get_or_schedule\(app, "([a-z_.]+)"', menu_src)))
    assert scheduled, "no cached rows to speak for"

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        for key in scheduled:
            lazy_trailing._CACHE[key] = ("STALE", 1e18)  # type: ignore[attr-defined]
        screen.refresh_items()
        for _ in range(6):
            await pilot.pause()
        survived = sorted(
            k
            for k in scheduled
            if (lazy_trailing._CACHE.get(k) or ("", 0))[0] == "STALE"  # type: ignore[attr-defined]
        )

    assert not survived, f"cached rows a repaint did not clear: {survived}"
