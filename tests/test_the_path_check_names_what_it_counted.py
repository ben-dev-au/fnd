"""`✓ 4 entries` beside a green tick read as "4 files will be indexed".

It is a non-recursive `iterdir` that counts subfolders, hidden files and
files a `no_index` tag keeps out. On a probe folder holding one indexable
file it said 4, and the number goes UP because a private file is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input, Static

from fnd.tui import FNDApp
from fnd.tui.settings_screen import AddCollectionWizard, EditBar, SettingsList
from tests._pilot_wait import wait_until


@pytest.mark.asyncio
async def test_the_count_names_the_folder_it_counted(tmp_path: Path, tmp_index_dir: Path) -> None:
    """One indexable file of four entries: the label must not promise four."""
    probe = tmp_path / "Probe"
    probe.mkdir()
    (probe / "a.md").write_text("# A\n\ntext.\n", encoding="utf-8")
    (probe / "b.md").write_text("---\ntags: [no_index]\n---\n\n# B\n", encoding="utf-8")
    (probe / "c.xyz").write_text("unsupported", encoding="utf-8")
    (probe / ".hidden.md").write_text("# H\n", encoding="utf-8")

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        # Gate on the ROW LIST, not on the screen object: the screen exists a
        # tick before it composes, and `query_one` then raises NoMatches.
        await wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, AddCollectionWizard) and bool(app.screen.query(SettingsList))
            ),
            timeout=30.0,
            message="the wizard never mounted its rows",
        )
        wiz = app.screen
        lst = wiz.query_one(SettingsList)
        lst.cursor_index = next(i for i, it in enumerate(lst._items) if it.id == "wiz.path")
        await pilot.press("enter")
        await pilot.pause()
        bar = wiz.query_one(EditBar)

        def status() -> str:
            return str(bar.query_one(".-edit-error", Static).render())

        bar.query_one("#editor_input", Input).value = str(probe)
        await wait_until(
            pilot,
            lambda: "✓" in status(),
            timeout=30.0,
            message="an existing path never validated",
        )
        text = status()

    assert "folder" in text, text
    assert text.index("folder") < text.index("4"), (
        f"the qualifier must survive a clip, so it comes first: {text!r}"
    )
