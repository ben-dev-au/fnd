"""Semantic search in the TUI: settings, rows found by meaning, landing, and the background job."""

from __future__ import annotations

import asyncio
import dataclasses
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

np = pytest.importorskip("numpy")

from fnd.config import Config, load  # noqa: E402
from fnd.embed_job import Progress  # noqa: E402
from fnd.extras import SEMANTIC  # noqa: E402
from fnd.extras_change import Change  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.query import FileGroup, Hit, Searcher  # noqa: E402
from fnd.tui.preview.presenter import landing_intent_from_node_data  # noqa: E402
from fnd.tui.results_labels import _format_file_label, _format_hit_label  # noqa: E402
from fnd.tui.results_view import ResultsView  # noqa: E402
from fnd.tui.semantic_service import SemanticService  # noqa: E402
from tests._semantic_fakes import build_context  # noqa: E402


def _hit(**changes: object) -> Hit:
    base = Hit(
        score=0.8,
        parent_id="p",
        path="/notes/orchestra.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="Orchestra > Solo",
        title="Orchestra",
        snippet="a piccolo solo",
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def test_a_meaning_row_carries_the_meaning_mark() -> None:
    """A section found by meaning starts with "≈" and never with "◌"."""
    label = str(_format_hit_label(_hit(found_by_meaning=True), max_score=1.0))
    assert "≈" in label
    assert "◌" not in label


def test_a_meaning_file_carries_the_meaning_mark() -> None:
    """A file only the model found is marked on its file row too."""
    h = _hit(found_by_meaning=True)
    group = FileGroup(parent_id="p", path=h.path, kind="md", title="", top_score=0.8, hits=[h])
    assert "≈" in str(_format_file_label(group, max_score=1.0))


def test_a_word_match_row_carries_no_meaning_mark() -> None:
    """Rows fnd found by words are unchanged."""
    assert "≈" not in str(_format_hit_label(_hit(), max_score=1.0))


def test_a_meaning_hit_never_reads_as_unlocatable() -> None:
    """No word highlight is expected for it, so the "◌" defect signal stays quiet."""
    from fnd.matching import MatchSpec

    spec = MatchSpec.from_query("flute", auto_fuzzy=False)
    hit = _hit(
        found_by_meaning=True,
        materialised=True,
        body_text="a piccolo solo",
        body_md="a piccolo solo",
    )
    assert ResultsView._match_visible(hit, spec, spec)


def test_a_meaning_row_lands_at_its_section_top() -> None:
    """Selecting a row found by meaning lands where an outline jump would."""
    meaning = _hit(found_by_meaning=True)
    assert landing_intent_from_node_data({"kind": "section", "hit": meaning}) == "chunk_top"
    group = FileGroup(
        parent_id="p", path=meaning.path, kind="md", title="", top_score=0.8, hits=[meaning]
    )
    assert landing_intent_from_node_data({"kind": "file", "group": group}) == "chunk_top"
    assert landing_intent_from_node_data({"kind": "section", "hit": _hit()}) is None


def _app(defaults: dict[str, Any] | None = None, collections: dict[str, bool] | None = None) -> Any:
    cols = {n: SimpleNamespace(semantic=on) for n, on in (collections or {"notes": True}).items()}
    d = SimpleNamespace(
        **{"semantic_search": True, "show_embedding_progress": True, **(defaults or {})}
    )
    return SimpleNamespace(
        _config=SimpleNamespace(defaults=d, collections=cols),
        _indexer=SimpleNamespace(task=None),
        _progress=SimpleNamespace(_active=None, begin=lambda plan: _Session()),
    )


class _Session:
    closed = False

    def __init__(self) -> None:
        self.reports: list[tuple[int, int]] = []
        self.labels: list[str] = []
        self.ended = ""

    def enter(self, phase: str) -> None: ...

    def report(self, done: int, total: int) -> None:
        self.reports.append((done, total))

    def set_label(self, label: str) -> None:
        self.labels.append(label)

    def close(self) -> None:
        self.ended = "close"

    def abandon(self) -> None:
        self.ended = "abandon"


class _Runner:
    def __init__(self) -> None:
        self.paused: list[bool] = []
        self.latest: Progress | None = None
        self.stopped = False
        self.running = True

    def pause(self, flag: bool) -> None:
        self.paused.append(flag)

    async def stop(self) -> None:
        self.stopped = True
        self.running = False

    def terminate(self) -> None:
        self.stopped = True


def test_no_context_while_switched_off(tmp_path: Path) -> None:
    """With semantic_search off, searches blend nothing even with vectors loaded."""
    service = SemanticService(_app({"semantic_search": False}), ready=lambda: True)
    service.use_context(object())  # type: ignore[arg-type]
    assert service.context() is None


def test_an_opt_out_narrows_the_context(tmp_path: Path, tmp_index_dir: Path) -> None:
    """A collection switched off takes no part from the next search on."""
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("# A\n\napple\n", encoding="utf-8")
    build_index(roots=[tmp_path / "notes"], index_dir=tmp_index_dir, collection="notes")
    ctx = build_context(Searcher(index_dir=tmp_index_dir), tmp_path / "v", opted_in={"notes"})
    service = SemanticService(_app(collections={"notes": False}), ready=lambda: True)
    service.use_context(ctx)
    narrowed = service.context()
    assert narrowed is not None
    assert narrowed.opted_in == frozenset()


def test_the_job_pauses_while_searching() -> None:
    """An interactive operation on the line pauses the job; its end resumes it."""
    app = _app()
    service = SemanticService(app, ready=lambda: True)
    runner = _Runner()
    service._runner = runner  # type: ignore[assignment]
    app._progress._active = SimpleNamespace(closed=False)
    service._update_pause()
    app._progress._active = None
    service._update_pause()
    assert runner.paused == [True, False]


def test_embedding_progress_shows_and_hides() -> None:
    """The line shows "Embedding n / m" unless show_embedding_progress is off."""
    shown = SemanticService(_app(), ready=lambda: True)
    shown._show(4, 10)
    assert shown._session is not None
    assert shown._session.labels == ["Embedding 4 / 10"]  # type: ignore[attr-defined]
    hidden = SemanticService(_app({"show_embedding_progress": False}), ready=lambda: True)
    hidden._show(4, 10)
    assert hidden._session is None


def test_a_stopped_run_is_not_shown_as_finished() -> None:
    """A run stopped part-way is abandoned on the line, a finished one closed."""
    service = SemanticService(_app(), ready=lambda: True)
    runner = _Runner()
    service._runner = runner  # type: ignore[assignment]
    service._show(4, 10)
    session = service._session
    runner.latest = Progress(4, 10)
    service._close_session()
    assert session.ended == "abandon"  # type: ignore[union-attr]
    service._show(10, 10)
    session = service._session
    runner.latest = Progress(10, 10)
    service._close_session()
    assert session.ended == "close"  # type: ignore[union-attr]


def _removal(folder: Path) -> Change:
    extra = dataclasses.replace(SEMANTIC, cache_dirs=lambda: [folder])
    return Change(
        extra=extra,
        removing=True,
        commands=[],
        files={},
        folders=frozenset(),
        dists=frozenset(),
        tools=frozenset(),
    )


def test_uninstall_stops_the_job_and_lets_go_of_its_files_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removal stops the job and releases the model and vectors before any command or delete."""
    from fnd.tui import settings_screen

    order: list[str] = []
    folder = tmp_path / "vectors"
    folder.mkdir()

    async def stop() -> None:
        order.append("stopped")

    service = SimpleNamespace(
        stop=stop, release=lambda: order.append("released"), settings_changed=lambda: None
    )

    def start(app: object, **kw: Any) -> None:
        order.append("install started")
        kw["on_done"]()

    monkeypatch.setattr("fnd.tui.extras_install_progress.start_extras_install", start)
    monkeypatch.setattr("fnd.tui.menu._setting_writer", lambda path: lambda app, v: None)
    asyncio.run(
        settings_screen.change_extra(
            SimpleNamespace(_semantic=service), _removal(folder), label="Uninstall"
        )
    )
    assert order == ["stopped", "released", "install started"]
    assert not folder.exists()


@pytest.mark.asyncio
async def test_the_install_modal_names_the_extra(tmp_index_dir: Path) -> None:
    """Installing semantic search says so, with its own closing note."""
    from fnd.tui import FNDApp
    from fnd.tui.extras_install_progress import ProgressEvent, start_extras_install

    app = FNDApp(index_dir=tmp_index_dir, config=Config())
    async with app.run_test(size=(120, 32)) as pilot:
        start_extras_install(
            app,
            cmds=[],
            action_label="Install",
            extra_name="semantic",
            done_note="Embedding starts now.",
        )
        await pilot.pause()
        box = app.screen.query_one("#extras_box")
        assert str(box.border_title) == "Install semantic"
        app.screen._render_event(ProgressEvent(phase="done", cmd_index=0, cmd_total=1))  # type: ignore[attr-defined]
        status = app.screen.query_one("#extras_status")
        assert "Embedding starts now." in str(status.render())
        # extras_finished revises the note when a folder waits; the modal shows the revision.
        app._extras_done_note = "Its last files go when fnd next starts."  # type: ignore[attr-defined]
        app.screen._render_event(ProgressEvent(phase="done", cmd_index=0, cmd_total=1))  # type: ignore[attr-defined]
        assert "Its last files go when fnd next starts." in str(status.render())


def test_the_progress_line_shows_time_left_once_measured() -> None:
    """The embedding label gains a time left from this machine's own rate."""
    now = [0.0]
    app = _app()
    service = SemanticService(app, ready=lambda: True, clock=lambda: now[0])
    service._show(100, 2000)
    now[0] = 30.0
    service._show(400, 2000)
    assert service._session is not None
    assert service._session.labels[-1] == "Embedding 400 / 2,000 · ~3 min left"  # type: ignore[attr-defined]


def test_the_install_screen_makes_no_fixed_rate_guess() -> None:
    """Before install nothing has been measured here, so no minutes are claimed."""
    from fnd.tui.settings_screen import install_summary

    text = install_summary(2000)
    assert "min" not in text
    assert "2,000 sections" in text
    assert "time left" in text


def test_a_job_that_found_the_store_busy_is_tried_again() -> None:
    """Another writer had the store (the CLI, a second fnd): retry later, never treat it as done."""
    from fnd.embed_job import BUSY_EXIT

    app = _app()
    timers: list[tuple[float, str]] = []
    app.set_interval = lambda *a, **k: SimpleNamespace(stop=lambda: None)
    app.set_timer = lambda delay, fn: timers.append((delay, fn.__name__))
    runner = _Runner()
    runner.exit_code = BUSY_EXIT  # type: ignore[attr-defined]

    async def start(opted: object) -> None: ...

    async def progress():  # type: ignore[no-untyped-def]
        return
        yield

    runner.start = start  # type: ignore[attr-defined]
    runner.progress = progress  # type: ignore[attr-defined]
    service = SemanticService(app, ready=lambda: True, runner_factory=lambda: runner)  # type: ignore[arg-type,return-value]
    service._load_context = lambda: None  # type: ignore[method-assign]
    asyncio.run(service._run_job())
    assert timers == [(60.0, "ensure_job")]


def test_quitting_stops_the_job_without_waiting() -> None:
    """Quit asks the job to stop at its next batch and returns at once."""
    service = SemanticService(_app(), ready=lambda: True)
    runner = _Runner()
    service._runner = runner  # type: ignore[assignment]
    service.stop_now()
    assert runner.stopped


@pytest.fixture
def notes(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "band.md").write_text("# Band\n\n## Kit\n\ndrum and a flute\n", encoding="utf-8")
    (root / "orchestra.md").write_text(
        "# Orchestra\n\n## Solo\n\na piccolo solo\n", encoding="utf-8"
    )
    for i in range(6):
        (root / f"other{i}.md").write_text(
            f"# Other {i}\n\nboat {'gourd ' * (i + 1)}\n", encoding="utf-8"
        )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [defaults]
            semantic_search = true

            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    return load(cfg_path)


@pytest.mark.asyncio
async def test_a_model_found_file_shows_and_lands_cleanly(
    notes: Config, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """End to end: a file without the word appears marked "≈", and its preview claims no lost match."""
    from textual.widgets import Tree

    from fnd.tui import FNDApp
    from tests._pilot_wait import wait_until

    ctx = build_context(Searcher(index_dir=tmp_index_dir), tmp_path / "v", opted_in={"notes"})
    app = FNDApp(index_dir=tmp_index_dir, config=notes, collection="notes")
    async with app.run_test(size=(120, 32)) as pilot:
        app._semantic.use_context(ctx)
        app._search.run("flute")
        tree = app.query_one("#results_pane", Tree)
        await wait_until(
            pilot,
            lambda: any("orchestra" in str(n.label) for n in tree.root.children),
            timeout=30.0,
            message="the model-found file never appeared",
        )
        node = next(n for n in tree.root.children if "orchestra" in str(n.label))
        assert "≈" in str(node.label)
        assert node.data is not None
        pid = node.data["group"].parent_id
        tree.move_cursor(node)

        def landed() -> bool:
            anchor = app._preview_scroll.anchor
            return (
                anchor is not None
                and anchor.parent_id == pid
                and bool(app._preview.chunk_cache.get(pid))
                and app._preview_scroll.is_armed
            )

        await wait_until(
            pilot, landed, timeout=30.0, message="the preview never landed on the file"
        )
        anchor = app._preview_scroll.anchor
        assert anchor is not None
        assert anchor.intent == "chunk_top"
        assert not app._current_match_unlocatable()


@pytest.mark.asyncio
async def test_the_semantic_rows_switch_it_and_tell_the_service(
    notes: Config, tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings' Semantic search toggle writes the config and re-evaluates the job."""
    from fnd.tui import FNDApp
    from fnd.tui.menu import _semantic_items

    app = FNDApp(index_dir=tmp_index_dir, config=notes, collection="notes")
    async with app.run_test(size=(120, 32)):
        calls: list[str] = []
        monkeypatch.setattr(app._semantic, "settings_changed", lambda: calls.append("changed"))
        toggle = next(i for i in _semantic_items() if i.id == "semantic.enabled")
        assert toggle.toggle_getter(app) is True  # type: ignore[misc]
        toggle.toggle_setter(app, False)  # type: ignore[misc]
        assert load(tmp_path / "config.toml").defaults.semantic_search is False
        assert calls == ["changed"]
        status = next(i for i in _semantic_items() if i.id == "semantic.status")
        assert status.kind == "display"


@pytest.mark.asyncio
async def test_a_collection_can_opt_out(
    notes: Config, tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collection's Semantic search row writes `semantic = false`."""
    from fnd.tui import FNDApp
    from fnd.tui.menu import _provider_collection

    app = FNDApp(index_dir=tmp_index_dir, config=notes, collection="notes")
    async with app.run_test(size=(120, 32)):
        monkeypatch.setattr(app._semantic, "settings_changed", lambda: None)
        row = next(i for i in _provider_collection(app, "notes") if i.id == "col.notes.semantic")
        row.toggle_setter(app, False)  # type: ignore[misc]
        assert load(tmp_path / "config.toml").collections["notes"].semantic is False


@pytest.mark.asyncio
async def test_a_dotted_collection_name_opts_out_whole(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collection named with a dot is written under its own name, not split at the dot."""
    from fnd.tui import FNDApp
    from fnd.tui.menu import _provider_collection

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[[collections."Notes v1.2".sources]]\npath = "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    app = FNDApp(index_dir=tmp_index_dir, config=load(cfg_path), collection="Notes v1.2")
    async with app.run_test(size=(120, 32)):
        monkeypatch.setattr(app._semantic, "settings_changed", lambda: None)
        row = next(
            i for i in _provider_collection(app, "Notes v1.2") if i.id == "col.Notes v1.2.semantic"
        )
        row.toggle_setter(app, False)  # type: ignore[misc]
        assert load(cfg_path).collections["Notes v1.2"].semantic is False


def test_a_moved_index_brings_the_vectors_up_to_date() -> None:
    """When the index has moved past the vectors' commit, the next search starts a reconcile."""
    app = _app()
    calls: list[str] = []
    app._search = SimpleNamespace(searcher=SimpleNamespace(generation=("idx", 2, 2)))
    app.call_later = lambda fn: calls.append(fn.__name__)
    service = SemanticService(app, ready=lambda: True)
    rows = SimpleNamespace(stamp=("idx", 1, 1))
    service.use_context(SimpleNamespace(rows=rows, opted_in=frozenset({"notes"})))  # type: ignore[arg-type]
    service.context()
    service.context()
    assert calls == ["ensure_job"]


def test_opting_in_during_a_run_restarts_it() -> None:
    """A collection switched on mid-run is embedded by a restarted job, not left for next launch."""
    app = _app()
    order: list[str] = []
    workers: list[object] = []
    app.run_worker = lambda coro, **kw: workers.append(coro)
    service = SemanticService(app, ready=lambda: True)
    service._embedder = object()  # type: ignore[assignment]
    runner = _Runner()
    service._runner = runner  # type: ignore[assignment]

    async def stop() -> None:
        order.append("stopped")
        runner.running = False

    service.stop = stop  # type: ignore[method-assign]
    service.ensure_job = lambda: order.append("started")  # type: ignore[method-assign]
    service.settings_changed()
    assert len(workers) == 1
    asyncio.run(workers[0])  # type: ignore[arg-type]
    assert order == ["stopped", "started"]
