"""Render ``mermaid`` fences as terminal text-art via termaid, off the event loop.

termaid's layout and routing can run for minutes, or loop without end while
memory grows, on a few bytes of crafted source, and a thread cannot be stopped.
So diagrams render in one long-lived worker process (:mod:`fnd._mermaid_worker`)
with a per-diagram time limit and a memory cap; a worker that hits either is
killed and replaced on next use. ``prepare`` fills the cache before a markdown
widget builds its blocks, and ``render`` only reads it, so a fence never renders
on the event loop. ``None`` means "fall back to source": not prepared, failed,
timed out, or not a diagram termaid draws.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

from rich.color import Color, ColorType
from rich.errors import StyleSyntaxError
from rich.style import Style
from rich.terminal_theme import TerminalTheme
from rich.text import Span, Text

try:
    import termaid
except Exception:  # pragma: no cover - optional dep absent
    termaid = None  # type: ignore[assignment]

_AVAILABLE = termaid is not None

#: A legitimate 120-edge flowchart renders in about 0.6 s.
RENDER_TIMEOUT_S: Final = 2.0
#: What one section may spend; sources it leaves unrendered show as source.
PREPARE_BUDGET_S: Final = 5.0
#: The art is converted and painted on the event loop, at about 3 us a character:
#: 2,000 sequence lines draw 1.1 M characters and stall it 3 s.
MAX_SOURCE_LINES: Final = 200
MAX_ART_CHARS: Final = 200_000
#: Covers the interpreter start and termaid's import, measured near 70 ms warm.
WORKER_START_TIMEOUT_S: Final = 15.0
WORKER_MEMORY_LIMIT: Final = 512 * 1024 * 1024
_CACHE_SIZE: Final = 256
_WORKER_SCRIPT: Final = Path(__file__).resolve().parents[1] / "_mermaid_worker.py"

_Key = tuple[str, str, bool]


class _Worker:
    """The render process, started on first use and replaced after any failure."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        atexit.register(self.stop)

    def render(self, key: _Key) -> Text | None:
        proc = self._proc if self._proc is not None and self._proc.poll() is None else None
        proc = proc or self._start()
        if proc is None:
            return None
        source, theme, use_ascii = key
        request = json.dumps({"source": source, "theme": theme, "use_ascii": use_ascii})
        reply = _exchange(proc, request + "\n", RENDER_TIMEOUT_S)
        if reply is None:
            self.stop()
            return None
        return _text_from(reply.get("art"))

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()

    def _start(self) -> subprocess.Popen[str] | None:
        self.stop()
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    # -P keeps fnd/ (struct.py and kin) off sys.path; -I would also
                    # hide a ``pip install --user`` termaid.
                    "-P",
                    str(_WORKER_SCRIPT),
                    str(WORKER_MEMORY_LIMIT),
                    str(os.getpid()),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="ascii",
            )
        except OSError:
            return None
        self._proc = proc  # before "ready", so a cancel during start-up can kill it
        if _exchange(proc, "", WORKER_START_TIMEOUT_S) is None or self._proc is not proc:
            self.stop()
            proc.kill()
            proc.wait()
            return None
        return proc


def _exchange(
    proc: subprocess.Popen[str], request: str, timeout: float
) -> dict[str, object] | None:
    """Send ``request`` and read one reply line, killing ``proc`` at ``timeout``."""
    assert proc.stdin is not None
    assert proc.stdout is not None
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        if request:
            proc.stdin.write(request)
            proc.stdin.flush()
        line = proc.stdout.readline()
    except (OSError, ValueError):
        line = ""
    finally:
        timer.cancel()
    try:
        reply = json.loads(line) if line else None
    except json.JSONDecodeError:
        reply = None
    return reply if isinstance(reply, dict) else None


def _text_from(art: object) -> Text | None:
    if not isinstance(art, dict):
        return None
    try:
        plain = str(art["plain"])
        spans = [Span(int(a), int(b), str(style)) for a, b, style in art["spans"]]
        base = str(art["style"])
    except (KeyError, TypeError, ValueError):
        return None
    if len(plain) > MAX_ART_CHARS:
        return None
    # A crafted ``fill:#ggg`` reaches here as ``on #gggggg``, which Rich raises on at paint.
    spans = [span for span in spans if _parses(str(span.style))]
    return Text(plain, style=base if _parses(base) else "", spans=spans)


def _parses(style: str) -> bool:
    try:
        Style.parse(style)
    except StyleSyntaxError:
        return False
    return True


class MermaidRenderer:
    """Render mermaid source to a Rich ``Text`` diagram, or ``None``."""

    def __init__(self) -> None:
        self._worker = _Worker()
        self._results: OrderedDict[_Key, Text | None] = OrderedDict()
        self._lock = threading.Lock()
        # Its own single thread: one worker, and queued slow renders never hold
        # the shared executor that Textual parses markdown on.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mermaid")
        self._epoch = 0

    def render(self, source: str, theme: str = "default", use_ascii: bool = False) -> Text | None:
        """The diagram :meth:`prepare` made for ``source``; never renders itself."""
        if not _renderable(source):
            return None
        key = (source, theme, use_ascii)
        with self._lock:
            if key not in self._results:
                return None
            self._results.move_to_end(key)
            return self._results[key]

    def is_prepared(self, source: str, theme: str = "default", use_ascii: bool = False) -> bool:
        if not _renderable(source):
            return True
        with self._lock:
            return (source, theme, use_ascii) in self._results

    async def prepare(
        self, sources: Iterable[str], theme: str = "default", use_ascii: bool = False
    ) -> None:
        """Render every unprepared source in the worker; a failure caches ``None``."""
        keys = [
            (s, theme, use_ascii)
            for s in dict.fromkeys(sources)
            if not self.is_prepared(s, theme, use_ascii)
        ]
        if keys:
            loop = asyncio.get_running_loop()
            job = loop.run_in_executor(self._executor, self._render_all, keys, self._epoch)
            # Shielded: a cancelled preview build still leaves the result cached.
            await asyncio.shield(job)

    def cancel_pending(self) -> None:
        """Drop queued renders and kill the one running, so quitting waits on neither."""
        self._epoch += 1
        self._worker.stop()

    def _render_all(self, keys: list[_Key], epoch: int) -> None:
        deadline = time.monotonic() + PREPARE_BUDGET_S
        for key in keys:
            if epoch != self._epoch or time.monotonic() > deadline:
                return
            if self.is_prepared(*key):
                continue
            art = self._worker.render(key)
            if epoch != self._epoch:
                return  # killed by cancel_pending, not by its own source
            with self._lock:
                self._results[key] = art
                while len(self._results) > _CACHE_SIZE:
                    self._results.popitem(last=False)


def _renderable(source: str) -> bool:
    return _AVAILABLE and bool(source.strip()) and source.count("\n") < MAX_SOURCE_LINES


def in_rgb(art: Text, theme: TerminalTheme) -> Text:
    """``art`` with its ANSI palette colours resolved to RGB through ``theme``.

    Textual's opacity blend skips ANSI colours, so a diagram drawn in them
    shows through a preview hidden at opacity 0.
    """
    out = art.copy()
    out.spans = [Span(s.start, s.end, _style_in_rgb(s.style, theme)) for s in art.spans]
    return out


def _style_in_rgb(style: str | Style, theme: TerminalTheme) -> str | Style:
    try:
        parsed = Style.parse(style) if isinstance(style, str) else style
    except StyleSyntaxError:
        return style
    fg, bg = _colour_in_rgb(parsed.color, theme, True), _colour_in_rgb(parsed.bgcolor, theme, False)
    return parsed + Style(color=fg, bgcolor=bg)


def _colour_in_rgb(colour: Color | None, theme: TerminalTheme, foreground: bool) -> Color | None:
    if colour is None or colour.type != ColorType.STANDARD:
        return None
    return Color.from_triplet(colour.get_truecolor(theme, foreground=foreground))
