"""Regression: flat per-line chunks must carry a plain ``str`` in ``fnd_text``.

``_mount_plain_chunk`` renders each body line to a rich ``Text`` (for display)
and stashed that same ``Text`` on ``line.fnd_text``. Match counting
(``MatchNavigator._count_stops``) and stop-region scanning
(``enumerate_stop_regions``) then feed ``fnd_text`` to ``text_has_any_match``,
which runs ``DOC_WORD_RE.finditer`` over it — ``re`` needs a ``str``, so a
rich ``Text`` raised ``TypeError: expected string or bytes-like object, got
'Text'`` and crashed the query (e.g. a wine PDF's "SPAIN" line while
searching ``bastardo``).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.containers import VerticalScroll

from fnd.extract.base import Block
from fnd.index import build_index
from fnd.matching import MatchSpec
from fnd.query import FileChunk
from fnd.tui import FNDApp
from fnd.tui.preview_scroll import enumerate_stop_regions


def _plain_chunk() -> FileChunk:
    return FileChunk(
        parent_id="wine0001",
        path="/x/winelist.txt",
        kind="plain",
        page=1,
        slide=0,
        heading_path="",
        chunk_seq=0,
        blocks=[Block(kind="p", text="SPAIN\nbastardo is a red grape variety\n")],
    )


@pytest.fixture
def min_app(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> FNDApp:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\nhello world\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{docs.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    from fnd.config import load

    build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")
    return FNDApp(config=load(cfg_path), index_dir=tmp_index_dir, collection="notes")


@pytest.mark.asyncio
async def test_plain_chunk_lines_carry_str_fnd_text(min_app: FNDApp) -> None:
    app = min_app
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = app.query_one("#preview_pane", VerticalScroll)
        app._search.current_query = "bastardo"
        app._search.match_spec = MatchSpec.from_query("bastardo")

        app._preview._mount_plain_chunk(pane, _plain_chunk())
        await pilot.pause()

        lines = list(pane.query("Static.chunk-line"))
        assert lines, "plain chunk should mount per-line Static widgets"
        for line in lines:
            txt = getattr(line, "fnd_text", None)
            assert isinstance(txt, str), f"fnd_text must be str, got {type(txt).__name__}"

        # The exact crash site: scanning stops over fnd_text must not raise.
        regions = enumerate_stop_regions(pane, app._effective_match_spec)
        assert len(regions) >= 1
