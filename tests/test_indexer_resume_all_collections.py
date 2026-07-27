"""Auto-resume covers every collection, not one named ``default``.

``maybe_resume`` used to load exactly ``default.state.toml``. Anyone whose
collections are all named — the normal case — had the "Auto-resume on launch"
toggle read as on while doing nothing, and their interrupted runs left state
files that were never resumed and never cleaned up.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
from fnd.index_runner import IndexState, save_state, saved_states


def _state(collection: str, *, done: int, total: int, age_hours: float = 0.5) -> IndexState:
    stamp = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=age_hours)
    return IndexState(
        collection=collection,
        started_at=stamp.isoformat(timespec="seconds"),
        total_files=total,
        files_completed=done,
    )


def _write_state_with_stamp(path: Path, state: IndexState, stamp: str) -> None:
    """Persist ``state`` with an exact ``last_update``.

    ``save_state`` overwrites the stamp with second-precision "now", so
    writing three states in one test gives them all the same value and any
    ordering trivially reads as sorted. Rewrite the field afterwards to get
    distinct, asserted-on stamps."""
    save_state(path, state)
    path.write_text(
        path.read_text(encoding="utf-8").replace(state.last_update, stamp), encoding="utf-8"
    )


def test_saved_states_reads_every_collection_newest_first(tmp_path: Path) -> None:
    stamps = {
        "alpha": "2026-07-20T09:00:00+00:00",
        "beta": "2026-07-27T11:00:00+00:00",
        "gamma": "2026-07-24T10:00:00+00:00",
    }
    # Deliberately written oldest-last so filename order (alpha, beta, gamma)
    # can't be mistaken for recency order.
    for name, stamp in stamps.items():
        _write_state_with_stamp(
            tmp_path / f"{name}.state.toml", _state(name, done=1, total=9), stamp
        )
    from unittest.mock import patch

    with patch("fnd.index_runner.state_dir", return_value=tmp_path):
        states = saved_states()

    assert [s.collection for _p, s in states] == ["beta", "gamma", "alpha"]
    assert [s.last_update for _p, s in states] == [stamps[n] for n in ("beta", "gamma", "alpha")]


def test_saved_states_skips_unreadable_files(tmp_path: Path) -> None:
    save_state(tmp_path / "ok.state.toml", _state("ok", done=1, total=4))
    (tmp_path / "broken.state.toml").write_text("not toml {{{", encoding="utf-8")
    from unittest.mock import patch

    with patch("fnd.index_runner.state_dir", return_value=tmp_path):
        states = saved_states()
    assert [s.collection for _p, s in states] == ["ok"]


class _StubApp:
    def __init__(self, cfg: Config) -> None:
        self._config = cfg
        self._index_dir = Path("/tmp/idx")
        self.started: list[str] = []
        self.notices: list[str] = []

    def start_indexer(self, *, collection: str, **_kw: Any) -> bool:
        self.started.append(collection)
        return True

    def notify(self, message: str, **_kw: Any) -> None:
        self.notices.append(message)


def _service(tmp_path: Path, cfg: Config) -> Any:
    from fnd.tui.indexer_service import IndexerService

    return IndexerService(_StubApp(cfg))  # type: ignore[arg-type]


def _cfg(names: list[str], *, auto_resume: bool = True) -> Config:
    return Config(
        defaults=Defaults(indexer_auto_resume=auto_resume),
        collections={
            n: CollectionConfig(sources=[SourceConfig(path=Path("/tmp") / n)]) for n in names
        },
    )


def test_named_collection_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(["CPL"])
    save_state(tmp_path / "CPL.state.toml", _state("CPL", done=3, total=97))
    monkeypatch.setattr("fnd.index_runner.state_dir", lambda: tmp_path)
    monkeypatch.setattr("fnd.config.load", lambda *_a, **_k: cfg)

    svc = _service(tmp_path, cfg)
    svc.maybe_resume()
    assert svc._app.started == ["CPL"]
    assert "CPL" in svc._app.notices[0]


def test_remaining_collections_queue_behind_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted Update-all should pick up where it stopped, not just
    resume its first collection and drop the rest."""
    cfg = _cfg(["CPL", "DPC", "SFO"])
    for name in ("CPL", "DPC", "SFO"):
        save_state(tmp_path / f"{name}.state.toml", _state(name, done=1, total=50))
    monkeypatch.setattr("fnd.index_runner.state_dir", lambda: tmp_path)
    monkeypatch.setattr("fnd.config.load", lambda *_a, **_k: cfg)

    svc = _service(tmp_path, cfg)
    svc.maybe_resume()
    assert len(svc._app.started) == 1
    assert svc.chain_total == 3
    assert sorted([*svc._app.started, *svc.chain_remaining]) == ["CPL", "DPC", "SFO"]


def test_state_for_a_deleted_collection_is_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(["CPL"])
    ghost = tmp_path / "OldCourse.state.toml"
    save_state(ghost, _state("OldCourse", done=2, total=40))
    monkeypatch.setattr("fnd.index_runner.state_dir", lambda: tmp_path)
    monkeypatch.setattr("fnd.config.load", lambda *_a, **_k: cfg)

    svc = _service(tmp_path, cfg)
    svc.maybe_resume()
    assert not ghost.exists(), "orphaned state file left behind"
    assert svc._app.started == []


def test_completed_state_is_cleaned_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(["CPL"])
    finished = tmp_path / "CPL.state.toml"
    save_state(finished, _state("CPL", done=97, total=97))
    monkeypatch.setattr("fnd.index_runner.state_dir", lambda: tmp_path)
    monkeypatch.setattr("fnd.config.load", lambda *_a, **_k: cfg)

    svc = _service(tmp_path, cfg)
    svc.maybe_resume()
    assert not finished.exists()
    assert svc._app.started == []


def test_opt_out_still_sweeps_but_never_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Indexing is heavy: with auto-resume off nothing may start — but the
    dead state file is still tidied."""
    cfg = _cfg(["CPL"], auto_resume=False)
    save_state(tmp_path / "CPL.state.toml", _state("CPL", done=3, total=97))
    ghost = tmp_path / "Gone.state.toml"
    save_state(ghost, _state("Gone", done=1, total=5))
    monkeypatch.setattr("fnd.index_runner.state_dir", lambda: tmp_path)
    monkeypatch.setattr("fnd.config.load", lambda *_a, **_k: cfg)

    svc = _service(tmp_path, cfg)
    svc.maybe_resume()
    assert svc._app.started == []
    assert not ghost.exists()
    assert (tmp_path / "CPL.state.toml").exists(), "a resumable state must survive"
