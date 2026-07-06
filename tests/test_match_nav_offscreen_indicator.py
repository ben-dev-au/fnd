"""The original bug: a result whose matches span multiple viewports gave the
user no signal that off-screen matches existed. The preview border now shows
``↑a``/``↓b`` (in the active-pane accent) counting matches above/below the
current viewport, so a below-the-fold match is announced on load and the
arrows flip as the user navigates."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview_scrollbar import MatchAwareScroll
from tests._pilot_wait import wait_until

# A CRC table taller than the viewport: CRC in card 32's answer and card 86's
# question, ~50 rows apart so the second is well below the fold on load (the
# viewport can't hold both — the original bug's shape).
TALL_TABLE_MD = (
    "# Flashcards\n\n| # | Q | A |\n| --- | --- | --- |\n"
    + "".join(f"| {i} | filler question {i} | filler answer {i} |\n" for i in range(1, 32))
    + "| 32 | Ethernet Type II Frame? | link-layer frame with a CRC checksum |\n"
    + "".join(f"| {i} | filler question {i} | filler answer {i} |\n" for i in range(33, 86))
    + "| 86 | What is the Ethernet CRC field used for? | Cyclic Redundancy Check |\n"
    + "".join(f"| {i} | filler question {i} | filler answer {i} |\n" for i in range(87, 91))
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def tall_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    a.mkdir(parents=True, exist_ok=True)
    (a / "Cards.md").write_text(TALL_TABLE_MD, encoding="utf-8")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _subtitle(app: FNDApp) -> object:
    return app.query_one("#preview_pane", MatchAwareScroll)._border_subtitle


def _accent_triangle_present(subtitle: object) -> bool:
    """The subtitle Content carries a ▲/▼ view marker styled in the accent."""
    plain = getattr(subtitle, "plain", "")
    spans = getattr(subtitle, "spans", [])
    has_marker = "▼" in plain or "▲" in plain
    has_accent = any("accent" in str(getattr(s, "style", "")) for s in spans)
    return has_marker and has_accent


@pytest.mark.asyncio
async def test_offscreen_match_announced_and_flips_on_nav(cfg: Config, tall_index: Path) -> None:
    app = FNDApp(index_dir=tall_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        # On load the viewport lands on the first CRC; the second is a screenful
        # below in the SAME result — the awareness signal the original bug lacked.
        await wait_until(
            pilot,
            lambda: app._match_nav.below >= 1,
            timeout=30.0,
            message="off-screen match below the fold in the current result was never detected",
        )
        sub = _subtitle(app)
        assert "▼" in getattr(sub, "plain", ""), (
            f"no ▼ view marker on border: {getattr(sub, 'plain', '')!r}"
        )
        assert _accent_triangle_present(sub), "marker is not rendered in the accent colour"

        # Navigate to the next match within this result: the first match's
        # screenful is now above the fold, so the marker flips direction.
        app.action_nav_next_match()
        await wait_until(
            pilot,
            lambda: app._match_nav.above >= 1,
            timeout=30.0,
            message="after n, the passed match's view was not reported above the fold",
        )
        assert "▲" in getattr(_subtitle(app), "plain", "")
