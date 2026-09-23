"""`Markdown … · 0` was rendered as `Markdown …`, with no number at all.

A rule that matches nothing is the loudest thing the pane can say, and it said
it by falling silent, indistinguishable from `(24 of 40 types)`, where the
counts are deliberately withheld, and from a kind this source simply has none
of. Ground truth at the time: `walk_sources` yielded 0 files and the run had
just reported `0 / 0 files · 11 removed`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters.scan import SourceSample
from fnd.filters.tree_model import _kind_items


def _label(items: list[tuple[str, str, str]], kind: str) -> str:
    return next(label for _cat, id_, label in items if id_ == f"kind:{kind}")


def test_a_kind_the_source_has_and_the_rules_drop_reads_zero() -> None:
    sample = SourceSample(kinds={"md": 6}, kinds_kept={}, gated=True)

    label = _label(_kind_items(sample), "md")

    assert label.rstrip().endswith("·  0"), label


def test_a_kind_the_source_does_not_have_carries_no_count() -> None:
    """The control: every kind is offered, so a bare `· 0` on all forty of
    them is a wall of noise that says nothing about this source."""
    sample = SourceSample(kinds={"md": 6}, kinds_kept={"md": 6}, gated=True)

    items = _kind_items(sample)

    assert _label(items, "md").rstrip().endswith("·  6"), _label(items, "md")
    assert "·" not in _label(items, "pdf"), _label(items, "pdf")


def test_an_ungated_sample_is_unchanged() -> None:
    """No gate ran, so there is no "kept none" to report."""
    sample = SourceSample(kinds={"md": 3})

    items = _kind_items(sample)

    assert _label(items, "md").rstrip().endswith("·  3")
    assert "·" not in _label(items, "txt")


@pytest.mark.asyncio
async def test_a_truncated_scan_says_so_even_on_a_tagless_source(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A bare row positively means "none here", so the qualifier saying the
    scan stopped early has to be reachable, not last in an `elif` chain whose
    earlier arm always wins for a source with no tags (the per-source browser
    always passes that arm's note)."""
    from textual.widgets import Static

    from fnd.filters import FilterSpec
    from fnd.index import build_index
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import FilterBrowserScreen
    from tests._pilot_wait import wait_until

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\n\nrisotto.\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        screen = FilterBrowserScreen(
            title="Index filters",
            spec=FilterSpec(),
            gitignore=True,
            fndignore=True,
            no_tags_note="no tags found in this source",
            sample_provider=lambda _spec: SourceSample(kinds={"md": 3}, truncated=True),
            on_save=lambda *_a: None,
        )
        app.push_screen(screen)
        await wait_until(
            pilot,
            lambda: bool(screen.query("#filter_summary")),
            timeout=20.0,
            message="the summary never composed",
        )
        await wait_until(
            pilot,
            lambda: (
                "Outside the expression"
                in screen.query_one("#filter_summary", Static).render_line(0).text
            ),
            timeout=20.0,
            message="the summary never carried its notes",
        )
        painted = "\n".join(
            screen.query_one("#filter_summary", Static).render_line(i).text for i in range(4)
        )

    assert "partial scan" in painted, painted
