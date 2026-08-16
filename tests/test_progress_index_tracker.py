"""The indexing observer.

A background index (auto-resume on launch, or a modal the user sent to the
background) used to report nothing after its opening toast, so the machine
could churn for minutes with no indication. These tests drive the tracker
against stand-ins for ``IndexerService`` and the real per-page heartbeat
module.

The label is the interesting part: during a slow PDF the file counter does
not move at all, which is precisely the shape of "it looks frozen".
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from fnd.tui import live_progress
from fnd.tui.progress.facility import ProgressFacility, ProgressSession
from fnd.tui.progress.operations import INDEX, IndexProgressTracker
from tests._progress_stubs import StubBar


class StubTask:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class StubState:
    def __init__(self, total_files: int = 0, files_completed: int = 0) -> None:
        self.total_files = total_files
        self.files_completed = files_completed


class StubService:
    def __init__(self) -> None:
        self.task: Any = None
        self.state: Any = None
        self.collection: str = ""
        self.chain_total: int = 1
        self.chain_remaining: list[str] = []


class StubApp:
    def __init__(self) -> None:
        self._indexer = StubService()
        self.bar = StubBar()
        self._progress = ProgressFacility(self)  # type: ignore[arg-type]

    def query_one(self, _selector: Any) -> StubBar:
        return self.bar

    def set_interval(self, _interval: float, _callback: Any, name: str = "") -> Any:
        return None


@pytest.fixture(autouse=True)
def _quiet_heartbeats() -> Generator[None]:  # pyright: ignore[reportUnusedFunction]
    """The heartbeat store is module-global shared state between the extraction
    worker and the UI; clear it either side so a page counter can't leak."""
    live_progress.reset_session()
    yield
    live_progress.reset_session()


@pytest.fixture
def app() -> StubApp:
    return StubApp()


@pytest.fixture
def tracker(app: StubApp) -> IndexProgressTracker:
    return IndexProgressTracker(app)  # type: ignore[arg-type]


def test_no_run_means_nothing_to_show(tracker: IndexProgressTracker) -> None:
    assert tracker.sample(tracker.begin()) is False


def test_a_finished_run_releases_the_line(app: StubApp, tracker: IndexProgressTracker) -> None:
    app._indexer.task = StubTask(done=True)
    assert tracker.sample(tracker.begin()) is False


def test_the_scan_has_no_denominator_yet(app: StubApp, tracker: IndexProgressTracker) -> None:
    """Enumerating sources can take seconds on a cloud-backed vault and has no
    total until it finishes."""
    app._indexer.task = StubTask()
    session = tracker.begin()
    assert tracker.sample(session) is True
    assert session.phase == "scan"


def test_files_report_a_real_fraction(app: StubApp, tracker: IndexProgressTracker) -> None:
    app._indexer.task = StubTask()
    app._indexer.state = StubState(total_files=40, files_completed=10)
    session = tracker.begin()
    tracker.sample(session)
    assert session.phase == "files"
    scan_weight, files_weight = INDEX.weights()[0], INDEX.weights()[1]
    assert session.fraction == pytest.approx(scan_weight + files_weight * 0.25, abs=1e-6)


def _running(app: StubApp, *, collection: str = "CPL") -> None:
    app._indexer.task = StubTask()
    app._indexer.collection = collection
    app._indexer.state = StubState(total_files=43, files_completed=13)


def test_the_label_names_the_collection_and_the_count(
    app: StubApp, tracker: IndexProgressTracker
) -> None:
    _running(app)
    session = tracker.begin()
    tracker.sample(session)
    assert session.label == "CPL · 13 of 43 files"


def test_a_chain_says_which_collection_it_is_on(
    app: StubApp, tracker: IndexProgressTracker
) -> None:
    _running(app)
    app._indexer.chain_total = 4
    app._indexer.chain_remaining = ["A", "B"]
    session = tracker.begin()
    tracker.sample(session)
    assert session.label == "CPL (2 of 4) · 13 of 43 files"


def test_a_slow_pdf_adds_the_page_counter(app: StubApp, tracker: IndexProgressTracker) -> None:
    """Texturising one large PDF can run for minutes without the file counter
    moving; the page beat is the only thing that shows it is alive."""
    _running(app)
    live_progress.report_heartbeat(("file-start", "/a/b/Module_06.pdf"))
    live_progress.report_heartbeat(("total", 118))
    live_progress.report_heartbeat(("page", 39))
    session = tracker.begin()
    tracker.sample(session)
    assert session.label == "CPL · 13 of 43 files · Module_06.pdf · page 40 of 118"


def test_no_page_detail_when_nothing_is_being_texturised(
    app: StubApp, tracker: IndexProgressTracker
) -> None:
    """Value only: a markdown-only collection gets no page counter."""
    _running(app)
    session = tracker.begin()
    tracker.sample(session)
    assert "page" not in session.label


def test_the_fraction_only_moves_forwards_as_files_land(
    app: StubApp, tracker: IndexProgressTracker
) -> None:
    _running(app)
    session: ProgressSession = tracker.begin()
    seen: list[float] = []
    for done in (13, 20, 20, 31, 43):
        app._indexer.state.files_completed = done
        tracker.sample(session)
        seen.append(session.fraction)
    assert seen == sorted(seen)


# ── wiring: the service actually opens a session ─────────────────


@pytest.mark.asyncio
async def test_a_background_run_opens_an_ambient_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything above tests the tracker against stand-ins, which cannot see
    whether the service ever calls it. A background run — ``open_modal=False``,
    which is what auto-resume starts on launch — is the case with no other
    indication at all, so this pins the one line that connects them.

    The session must be AMBIENT: an interactive one is retired by the first
    navigation, and a reindex outlives hundreds of those.
    """
    from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
    from fnd.tui import FNDApp
    from fnd.tui.progress.model import OperationKind

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# a\n", encoding="utf-8")
    cfg = Config(
        defaults=Defaults(),
        collections={"default": CollectionConfig(sources=[SourceConfig(path=root)])},
    )
    monkeypatch.setattr("fnd.config.load", lambda: cfg)

    app = FNDApp(index_dir=tmp_path / "idx", config=cfg)
    async with app.run_test():
        started = app._indexer.start(
            collection="default", config=cfg.collections["default"], open_modal=False
        )
        assert started, "setup — the indexer did not start"
        session = app._progress.ambient
        assert session is not None, "a background index reported nothing on the line"
        assert session.operation_id == "index"
        assert session.kind is OperationKind.AMBIENT
        if app._indexer.cancel is not None:
            app._indexer.cancel.set()
