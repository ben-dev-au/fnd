"""Skip lines must not be printed into the terminal the TUI is painting.

``run_indexer`` printed ``[fnd skip …]`` to stderr unconditionally. Under
the TUI that is the same terminal Textual owns, so a run with failures
(one real corpus produced ~40 in a single collection) scribbles over the
rendered UI. The skip already reaches the user twice — as a ``file_error``
event and in the failure log — so the print belongs to the CLI alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.extract import ExtractError
from fnd.index_runner import run_indexer


def _always_fails(path: object, **_kw: object) -> object:
    raise ExtractError(str(path), "nope")


async def _run(tmp_path: Path, *, echo: bool) -> list[str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    (corpus / "boom.md").write_text("# a\n\nbody\n", encoding="utf-8")

    errors: list[str] = []
    async for ev in run_indexer(
        config=CollectionConfig(sources=[SourceConfig(path=corpus)]),
        collection="echo",
        index_dir=tmp_path / f"idx-{echo}",
        state_path=tmp_path / f"state-{echo}.toml",
        echo_skips=echo,
    ):
        if ev.kind == "file_error":
            errors.append(ev.error)
    return errors


@pytest.mark.asyncio
async def test_tui_path_stays_silent_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("fnd.index_runner.extract", _always_fails)
    errors = await _run(tmp_path, echo=False)
    assert errors, "the failure must still reach the caller as an event"
    assert "[fnd skip" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_cli_path_still_reports_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("fnd.index_runner.extract", _always_fails)
    errors = await _run(tmp_path, echo=True)
    assert errors
    assert "[fnd skip" in capsys.readouterr().err
