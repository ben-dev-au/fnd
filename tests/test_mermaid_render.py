import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from rich.console import Console
from rich.text import Text

import fnd.tui.mermaid_render as m
from fnd.tui.mermaid_render import MermaidRenderer

FLOW = "flowchart TD\n  A[Start] --> B[End]"
#: termaid never returns on this label and grows memory at about 280 MB/s.
LABEL_HANG = 'flowchart TD\n  A["`a*`"] --> B'
#: 900 edges from one statement: layout and routing run far past the time limit.
AMPERSAND_FAN = (
    "flowchart TD\n  "
    + " & ".join(f"a{i}" for i in range(30))
    + " --> "
    + " & ".join(f"b{i}" for i in range(30))
)


def _prepared(r: MermaidRenderer, source: str, **kw: object) -> Text | None:
    asyncio.run(r.prepare([source], **kw))  # type: ignore[arg-type]
    return r.render(source, **kw)  # type: ignore[arg-type]


def test_valid_flowchart_returns_text():
    out = _prepared(MermaidRenderer(), FLOW)
    assert isinstance(out, Text)
    assert "Start" in out.plain


def test_render_never_renders_an_unprepared_source(monkeypatch: pytest.MonkeyPatch):
    r = MermaidRenderer()
    monkeypatch.setattr(r._worker, "render", lambda key: pytest.fail("rendered on the caller"))
    assert r.render(FLOW) is None


def test_garbage_returns_none():
    assert _prepared(MermaidRenderer(), "this is not mermaid") is None


def test_empty_returns_none():
    assert _prepared(MermaidRenderer(), "   ") is None


def test_art_keeps_termaid_styles():
    out = _prepared(MermaidRenderer(), FLOW)
    assert out is not None
    assert out.spans
    assert out == m.termaid.render_rich(FLOW)


@pytest.mark.parametrize("hostile", [LABEL_HANG, AMPERSAND_FAN], ids=["label-hang", "fan"])
def test_hostile_diagram_falls_back_within_the_time_limit(hostile: str):
    r = MermaidRenderer()
    started = time.monotonic()
    assert _prepared(r, hostile) is None
    assert time.monotonic() - started < m.RENDER_TIMEOUT_S + 5
    assert _prepared(r, FLOW) is not None, "a fresh worker replaces the killed one"


def test_memory_cap_stops_a_runaway_render_before_the_time_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(m, "RENDER_TIMEOUT_S", 60.0)
    monkeypatch.setattr(m, "WORKER_MEMORY_LIMIT", 200 * 1024 * 1024)
    started = time.monotonic()
    assert _prepared(MermaidRenderer(), LABEL_HANG) is None
    assert time.monotonic() - started < 30


def test_a_large_parent_does_not_count_against_the_worker(monkeypatch: pytest.MonkeyPatch):
    """Linux keeps ru_maxrss across a vfork and exec: the worker must not inherit fnd's."""
    monkeypatch.setattr(m, "WORKER_MEMORY_LIMIT", 150 * 1024 * 1024)
    ballast = b"\x01" * (300 * 1024 * 1024)
    assert _prepared(MermaidRenderer(), FLOW) is not None
    assert len(ballast)


def test_a_failed_source_is_not_retried(monkeypatch: pytest.MonkeyPatch):
    r = MermaidRenderer()
    calls: list[str] = []
    monkeypatch.setattr(r._worker, "render", lambda key: calls.append(key[0]))
    _prepared(r, LABEL_HANG)
    _prepared(r, LABEL_HANG)
    assert calls == [LABEL_HANG]


def test_cache_hit_does_not_reinvoke(monkeypatch: pytest.MonkeyPatch):
    r = MermaidRenderer()
    real = r._worker.render
    calls: list[str] = []
    monkeypatch.setattr(r._worker, "render", lambda key: calls.append(key[0]) or real(key))
    assert _prepared(r, FLOW) is not None
    assert _prepared(r, FLOW) is not None
    assert calls == [FLOW]


def test_a_dead_worker_is_replaced():
    r = MermaidRenderer()
    assert _prepared(r, FLOW) is not None
    proc = r._worker._proc
    assert proc is not None
    proc.kill()
    proc.wait()
    assert _prepared(r, "sequenceDiagram\n  A->>B: hi") is not None


def test_ascii_mode_differs():
    r = MermaidRenderer()
    uni = _prepared(r, FLOW, use_ascii=False)
    asc = _prepared(r, FLOW, use_ascii=True)
    assert uni is not None
    assert asc is not None
    assert "┌" in uni.plain
    assert "┌" not in asc.plain


def test_oversized_source_falls_back_without_rendering(monkeypatch: pytest.MonkeyPatch):
    """Past the line cap the art would stall the loop to convert; it is never requested."""
    r = MermaidRenderer()
    monkeypatch.setattr(r._worker, "render", lambda key: pytest.fail("oversized source rendered"))
    huge = "flowchart TD\n" + "\n".join(f"  N{i} --> N{i + 1}" for i in range(m.MAX_SOURCE_LINES))
    assert r.is_prepared(huge)
    assert _prepared(r, huge) is None


def test_oversized_art_falls_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(m, "MAX_ART_CHARS", 50)
    assert _prepared(MermaidRenderer(), FLOW) is None


def test_an_unparseable_style_is_dropped_not_painted():
    """termaid turns ``fill:#ggg`` into ``on #gggggg``, which Rich raises on at paint."""
    out = _prepared(MermaidRenderer(), "flowchart TD\n  A[x] --> B[y]\n  style A fill:#ggg")
    assert out is not None
    assert "on #gggggg" not in {str(s.style) for s in out.spans}
    Console(file=io.StringIO(), color_system="truecolor").print(out)


def test_render_keeps_a_read_entry_from_eviction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(m, "_CACHE_SIZE", 3)
    r = MermaidRenderer()
    sources = [f"flowchart TD\n  A{i} --> B{i}" for i in range(4)]
    assert _prepared(r, sources[0]) is not None
    for s in sources[1:]:
        assert r.render(sources[0]) is not None
        _prepared(r, s)
    assert r.render(sources[0]) is not None


def test_cancel_pending_stops_queued_renders_promptly():
    """A quit mid-render waits for neither the running render nor the queued ones."""
    r = MermaidRenderer()
    hostile = [LABEL_HANG + f"\n  %% {i}" for i in range(5)]

    async def run() -> float:
        job = asyncio.ensure_future(r.prepare(hostile))
        await asyncio.sleep(0.5)
        started = time.monotonic()
        r.cancel_pending()
        await job
        return time.monotonic() - started

    assert asyncio.run(run()) < 1.0
    assert not any(r.is_prepared(s) for s in hostile), "a cancelled render is not a failure"


def test_a_section_stops_rendering_at_its_budget(monkeypatch: pytest.MonkeyPatch):
    """Sources left past the budget stay unprepared: shown as source, retried later."""
    monkeypatch.setattr(m, "PREPARE_BUDGET_S", 0.1)
    r = MermaidRenderer()
    asyncio.run(r.prepare([LABEL_HANG, FLOW]))
    assert r.is_prepared(LABEL_HANG)
    assert not r.is_prepared(FLOW)


def test_cancel_during_worker_start_up_is_prompt():
    """The worker is killable before its ready line, so a quit at once never waits."""
    r = MermaidRenderer()

    async def run() -> float:
        job = asyncio.ensure_future(r.prepare([LABEL_HANG]))
        await asyncio.sleep(0.01)
        started = time.monotonic()
        r.cancel_pending()
        await job
        return time.monotonic() - started

    assert asyncio.run(run()) < 1.0
    assert not r.is_prepared(LABEL_HANG)


@pytest.mark.skipif(not hasattr(signal, "SIGTSTP"), reason="no job control")
def test_the_worker_ignores_job_control_and_ctrl_c():
    """It shares fnd's process group: a Ctrl+Z must not stop it mid-render."""
    # Its own group under this process, so the group is not orphaned: the kernel
    # discards SIGTSTP sent to an orphaned group, which would pass vacuously.
    proc = subprocess.Popen(
        [sys.executable, "-P", str(m._WORKER_SCRIPT), str(m.WORKER_MEMORY_LIMIT), str(os.getpid())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        process_group=0,
    )
    try:
        assert proc.stdout is not None
        assert "ready" in proc.stdout.readline()
        proc.send_signal(signal.SIGTSTP)
        proc.send_signal(signal.SIGINT)
        time.sleep(0.2)
        pid, status = os.waitpid(proc.pid, os.WNOHANG | os.WUNTRACED)
        assert pid == 0, f"worker stopped or exited: status {status}"
    finally:
        os.kill(proc.pid, signal.SIGCONT)
        proc.kill()
        proc.wait()


def test_the_worker_refuses_oversized_art_before_serialising_it():
    """The reply carries a span per character, so a cap applied only on receipt is too late."""
    r = MermaidRenderer()
    assert _prepared(r, FLOW) is not None
    proc = r._worker._proc
    assert proc is not None
    request = json.dumps({"source": "flowchart TD\n  A[Other] --> B[End]", "max_chars": 50})
    reply = m._exchange(proc, request + "\n", m.RENDER_TIMEOUT_S)
    assert reply == {"art": None}


def test_a_long_single_line_source_falls_back_without_rendering(monkeypatch: pytest.MonkeyPatch):
    r = MermaidRenderer()
    monkeypatch.setattr(r._worker, "render", lambda key: pytest.fail("oversized source rendered"))
    long_label = "flowchart TD\n  A[" + "x" * m.MAX_SOURCE_CHARS + "] --> B"
    assert r.is_prepared(long_label)
    assert _prepared(r, long_label) is None


def test_a_prepare_from_before_a_cancel_renders_nothing(monkeypatch: pytest.MonkeyPatch):
    """A build that began before a quit's cancel cannot restart the worker."""
    r = MermaidRenderer()
    started = r.epoch
    r.cancel_pending()
    monkeypatch.setattr(r._worker, "render", lambda key: pytest.fail("rendered after cancel"))
    asyncio.run(r.prepare([FLOW], epoch=started))
    assert not r.is_prepared(FLOW)
