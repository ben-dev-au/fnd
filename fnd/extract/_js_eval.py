"""Evaluate an inline script's computed data in the OS JavaScript shell, confined.

Static reading (``_js_data``) cannot see values a page computes with its own
helpers (``ROWS.map(function (r) { return chip(r); })``). Where the OS ships a
shell (macOS: ``jsc``) this runs the page's script up to its last data literal
against a do-nothing DOM and reads the data objects back as JSON. The shell runs
in a child process under ``sandbox-exec`` (no network, no fork, no file writes,
no reads of user data), with an empty environment, a CPU and wall-clock limit and
a streamed output cap. Without the sandbox nothing runs. Any failure returns
nothing and the caller keeps the static values. ``FND_HTML_EVAL=0`` turns it off.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from fnd.extract._js_data import Assignment

TIMEOUT_SECONDS = 10
_MAX_OUTPUT = 8 * 1024 * 1024
_JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc")
_SANDBOX = Path("/usr/bin/sandbox-exec")
_PRIVATE_ROOTS = [
    *("/Users", "/Volumes", "/Applications", "/Library", "/opt", "/usr/local", "/home", "/cores"),
    *("/private/var", "/private/tmp", "/private/etc"),
]
_MAX_RSS_KB = 1024 * 1024
_WRAPPER = (
    'ulimit -t "$0"; "$@" & p=$!; (sleep "$0"; kill -9 $p) >/dev/null 2>&1 & w=$!; '
    "wait $p; s=$?; kill $w >/dev/null 2>&1; exit $s"
)
_NEXT_STATEMENT = re.compile(r"[ \t]*;")

_PRELUDE = r"""
var __print = print;
(function (g) {
  ["readFile", "writeFile", "read", "readline", "load", "loadString", "run", "runString", "write",
   "printErr", "debug", "quit", "createGlobalObject", "$", "$262", "setTimeout", "setInterval",
   "generateHeapSnapshot", "generateHeapSnapshotForGCDebugging", "startSamplingProfiler",
   "samplingProfilerStackTraces", "sleepSeconds", "callerSourceOrigin", "jscStack",
   "transferArrayBuffer", "checkSyntax", "checkModuleSyntax"].forEach(function (n) {
    try { delete g[n]; } catch (e) {}
    if (n in g) { try { g[n] = undefined; } catch (e) {} }
  });
})(this);
var __stub = (function () {
  var f = function () { return p; };
  var p = new Proxy(f, {
    get: function (t, k) {
      if (k === Symbol.toPrimitive) return function () { return ""; };
      if (k === "length") return 0;
      if (k === Symbol.iterator) return function* () {};
      return p;
    },
    set: function () { return true; }, apply: function () { return p; },
    construct: function () { return p; }, has: function () { return true; }
  });
  return p;
})();
var document = __stub, window = __stub, self = __stub, navigator = __stub, location = __stub,
    localStorage = __stub, sessionStorage = __stub, history = __stub, screen = __stub,
    IntersectionObserver = __stub, ResizeObserver = __stub, MutationObserver = __stub,
    requestAnimationFrame = __stub, matchMedia = __stub, getComputedStyle = __stub,
    setTimeout = __stub, setInterval = __stub, clearTimeout = __stub, fetch = __stub,
    console = { log: function () {}, warn: function () {}, error: function () {} };
function __get(fn) { try { return fn(); } catch (e) { return null; } }
function __dump(o) {
  var chain = [];
  __print("@@MARK@@" + JSON.stringify(o, function (k, v) {
    if (typeof v === "function") return undefined;
    if (v && typeof v === "object") {
      while (chain.length && chain[chain.length - 1] !== this) chain.pop();
      if (chain.indexOf(v) >= 0) return undefined;
      chain.push(v);
    }
    return v;
  }));
}
"""


def find_jsc() -> Path | None:
    if os.environ.get("FND_HTML_EVAL") == "0" or not _SANDBOX.is_file():
        return None
    return _JSC if _JSC.is_file() else None


def _profile(script: Path) -> str:
    roots = " ".join(f'(subpath "{r}")' for r in _PRIVATE_ROOTS)
    return (
        "(version 1)(allow default)(deny network*)(deny process-fork)"
        '(deny file-write*)(allow file-write* (literal "/dev/null"))'
        f'(deny file-read* {roots})(allow file-read* (literal "{script}"))'
    )


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)  # the wrapper shell, the sandbox and the script


def _group_rss_kb(pgid: int) -> int:
    """Resident memory of every process in the group, from ``ps`` (macOS has no cgroups)."""
    done = subprocess.run(
        ["/bin/ps", "-A", "-o", "pgid=,rss="], capture_output=True, text=True, check=False
    )
    total = 0
    for line in done.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(pgid) and fields[1].isdigit():
            total += int(fields[1])
    return total


def _run(cmd: list[str], cwd: str, timeout: float) -> bytes:
    """Child stdout read through a hard cap; the child dies on the cap, the deadline or memory."""
    buf = bytearray()
    with subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={},
        cwd=cwd,
        start_new_session=True,
    ) as proc:

        def pump() -> None:
            assert proc.stdout is not None
            fd = proc.stdout.fileno()
            while chunk := os.read(fd, 65536):
                buf.extend(chunk)
                if len(buf) > _MAX_OUTPUT:
                    _kill(proc)
                    return

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            while proc.poll() is None:
                if time.monotonic() >= deadline or _group_rss_kb(proc.pid) > _MAX_RSS_KB:
                    break
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(0.2)
        finally:
            _kill(proc)  # also reaps the watchdog's sleep, which outlives the script
            proc.wait()
            reader.join()
    return bytes(buf)


def _program(src: str, assigned: list[Assignment], mark: str) -> str:
    end = max(a.end for a in assigned)
    if m := _NEXT_STATEMENT.match(src, end):
        end = m.end()
    keys = {(a.base, a.name) for a in assigned}
    bases = {base for base, _ in keys}
    wanted = ",".join(
        f'"{base}:{name}": __get(function () {{ return {base + "." + name if base else name}; }})'
        for base, name in sorted(keys)
        if base not in ("window", "document", "self") and (base or name not in bases)
    )
    return (
        f"{_PRELUDE.replace('@@MARK@@', mark)}\n{src[:end]}\n;__dump({{{wanted}}});\n{src[end:]}\n"
    )


def _command(jsc: Path, script: Path, timeout: float) -> list[str]:
    # The watchdog outlives a killed parent, so a blocked script cannot linger past its budget.
    shell = ["/bin/sh", "-c", _WRAPPER, str(int(timeout) + 2)]
    return [*shell, str(_SANDBOX), "-p", _profile(script), str(jsc), str(script)]


def evaluate(
    src: str, assigned: list[Assignment], timeout: float | None = None
) -> dict[tuple[str, str], object]:
    """Evaluated value of each ``base.name`` / ``name`` assignment; empty on any failure."""
    jsc = find_jsc()
    timeout = TIMEOUT_SECONDS if timeout is None else timeout
    if jsc is None or not assigned or timeout <= 0:
        return {}
    mark = f"@@{secrets.token_hex(8)}@@"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        script = root / "page.js"
        script.write_text(_program(src, assigned, mark), encoding="utf-8")
        try:
            out = _run(_command(jsc, script, timeout), str(root), timeout)
        except OSError:
            return {}
    if len(out) > _MAX_OUTPUT:
        return {}
    return _read(out.decode("utf-8", "replace"), mark)


def _read(stdout: str, mark: str) -> dict[tuple[str, str], object]:
    for line in stdout.split("\n"):
        if line.startswith(mark):
            try:
                data = json.loads(line[len(mark) :])
            except (ValueError, RecursionError):
                return {}
            if not isinstance(data, dict):  # a page can print its own line carrying the mark
                return {}
            return {
                (key.partition(":")[0], key.partition(":")[2]): value
                for key, value in data.items()
                if value is not None
            }
    return {}
