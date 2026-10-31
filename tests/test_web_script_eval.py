"""Computed page data: evaluated in the OS JavaScript shell when there is one, confined."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from fnd.extract import _js_eval, extract

pytestmark = pytest.mark.skipif(_js_eval.find_jsc() is None, reason="no system JavaScript shell")

_HELPERS = """
(function () {
  var C = function (t, k) { return { t: t, k: k }; };
  var WORDS = ["alpha", "beta", "gamma"];
  var FLOWS = {};
  FLOWS.demo = { steps: [
    { say: "Each word becomes one chip in the row.",
      views: [{ type: "rows", label: "words",
                items: WORDS.map(function (w) { return C(w, "k"); }) }] },
    { say: "Counting the chips gives the length of the row.",
      views: [{ type: "out", cmd: "len(words)", out: "n = " + WORDS.length }] }
  ] };
  document.getElementById("never").addEventListener("click", function () {});
})();
"""


def _md(tmp_path: Path, script: str) -> str:
    p = tmp_path / "page.html"
    p.write_text(
        f'<html><body><h1>Doc</h1><h2>Demo</h2><div data-flow="demo"></div>'
        f"<script>{script}</script></body></html>",
        encoding="utf-8",
    )
    return "\n\n".join(c.body_md for c in extract(p))


def test_computed_chips_are_recovered(tmp_path: Path) -> None:
    md = _md(tmp_path, _HELPERS)
    assert "`alpha` `beta` `gamma`" in md


def test_computed_output_strings_are_recovered(tmp_path: Path) -> None:
    assert "n = 3" in _md(tmp_path, _HELPERS)


def test_a_value_shared_between_flows_is_recovered_in_each(tmp_path: Path) -> None:
    script = """(function () {
      var RED = ["red", "green"].map(function (t) { return { t: t }; });
      var FLOWS = {};
      FLOWS.demo = { steps: [{ say: "First flow shows the shared row.",
        views: [{ type: "rows", label: "a", items: RED }] }] };
      FLOWS.other = { steps: [{ say: "Second flow shows the same row.",
        views: [{ type: "rows", label: "b", items: RED }] }] };
    })();"""
    assert _md(tmp_path, script).count("`red` `green`") == 2


def test_evaluation_can_be_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FND_HTML_EVAL", "0")
    md = _md(tmp_path, _HELPERS)
    assert "`alpha`" not in md
    assert "Each word becomes one chip" in md


def test_a_page_cannot_read_local_files(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("hunter2-token", encoding="utf-8")
    script = (
        "var FLOWS = {}; FLOWS.demo = { steps: [{ say: 'Reading a file should not work here.',"
        f" views: [{{ type: 'out', cmd: 'x', out: readFile('{secret}') }}] }}] }};"
    )
    assert "hunter2-token" not in _md(tmp_path, script)


def _confined(tmp_path: Path, js: str, timeout: float = 5) -> bytes:
    """Run raw JS (no prelude) through the same sandboxed command the evaluator uses."""
    jsc = _js_eval.find_jsc()
    assert jsc is not None
    root = tmp_path.resolve()
    script = root / "raw.js"
    script.write_text(js, encoding="utf-8")
    return _js_eval._run(_js_eval._command(jsc, script, timeout), str(root), timeout)


def test_the_os_sandbox_blocks_writes_even_with_the_shell_function_present(tmp_path: Path) -> None:
    target = tmp_path / "pwned.txt"
    out = _confined(tmp_path, f"writeFile('{target.resolve()}', 'x'); print('ran');")
    assert not target.exists()
    assert b"ran" not in out


def test_the_os_sandbox_blocks_reading_user_files_and_dynamic_import(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("hunter2-token", encoding="utf-8")
    js = (
        f"try {{ print(readFile('{secret.resolve()}')); }} catch (e) {{ print('read blocked'); }}\n"
        f"import('{secret.resolve()}').then(function () {{ print('IMPORTED'); }}, function () {{}});\n"
    )
    out = _confined(tmp_path, js)
    assert b"hunter2-token" not in out
    assert b"IMPORTED" not in out
    assert b"read blocked" in out


def test_the_unsandboxed_shell_could_write_so_the_sandbox_is_what_stops_it(tmp_path: Path) -> None:
    target = tmp_path / "control.txt"
    script = tmp_path / "control.js"
    script.write_text(f"writeFile('{target}', 'x');", encoding="utf-8")
    jsc = _js_eval.find_jsc()
    assert jsc is not None
    _js_eval._run([str(jsc), str(script)], str(tmp_path), 5)
    assert target.exists()


def test_output_is_capped_while_streaming_not_after_buffering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_js_eval, "_MAX_OUTPUT", 1024 * 1024)
    out = _confined(tmp_path, "var s = 'x'.repeat(1 << 20); while (true) { print(s); }")
    assert len(out) <= 1024 * 1024 + 65536 + 1024 * 1024


def test_a_script_that_allocates_without_limit_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_js_eval, "_MAX_RSS_KB", 256 * 1024)
    started = time.monotonic()
    _confined(tmp_path, "var a = []; while (true) { a.push(new Array(1 << 20).fill(1)); }", 30)
    assert time.monotonic() - started < 10


def test_a_blocked_script_cannot_outlive_a_killed_parent(tmp_path: Path) -> None:
    jsc = _js_eval.find_jsc()
    assert jsc is not None
    script = tmp_path.resolve() / "block.js"
    script.write_text("Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);")
    proc = subprocess.Popen(
        _js_eval._command(jsc, script, 1),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.5)
    proc.kill()  # the shell only: what a crashed parent leaves behind
    proc.wait()
    assert _js_eval._group_rss_kb(proc.pid) > 0  # the script is still running, orphaned
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and _js_eval._group_rss_kb(proc.pid) > 0:
        time.sleep(0.25)
    assert _js_eval._group_rss_kb(proc.pid) == 0


def test_a_line_separator_in_evaluated_data_does_not_lose_the_evaluation(tmp_path: Path) -> None:
    script = (
        "var FLOWS = {}; FLOWS.demo = { steps: [{ say: 'Narration for the step.', views: ["
        "{ type: 'out', cmd: 'x', out: [1].map(function () { return 'first\\u2028second'; })[0] }] }] };"
    )
    assert "first" in _md(tmp_path, script)


def test_a_lone_surrogate_in_evaluated_data_is_valid_text(tmp_path: Path) -> None:
    script = (
        "var FLOWS = {}; FLOWS.demo = { steps: [{ say: 'Narration for the step.', views: ["
        "{ type: 'out', cmd: 'x', out: [1].map(function () { return '\\ud83d lone after eval'; })[0] }] }] };"
    )
    p = tmp_path / "page.html"
    p.write_text(
        f'<html><body><h1>Doc</h1><div data-flow="demo"></div><script>{script}</script></body></html>',
        encoding="utf-8",
    )
    chunks = list(extract(p))
    for c in chunks:
        (c.body + c.body_md + c.heading_path).encode("utf-8")
    assert "lone after eval" in "\n".join(c.body for c in chunks)


def test_evaluation_still_runs_when_the_prose_lives_in_another_literal(tmp_path: Path) -> None:
    script = (
        "var NOTES = ['Each retry waits twice as long as the last one.'];"
        "var FLOWS = {}; FLOWS.demo = { steps: NOTES.map(function (n) { return { say: n }; }) };"
    )
    assert "Each retry waits twice" in _md(tmp_path, script)


def test_nothing_is_left_running_after_an_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids: list[int] = []
    real = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        proc = real(*args, **kwargs)
        pids.append(proc.pid)
        return proc

    monkeypatch.setattr(_js_eval.subprocess, "Popen", spy)
    _confined(tmp_path, "print('done');")
    assert pids
    assert _js_eval._group_rss_kb(pids[0]) == 0


def test_a_runaway_script_times_out_and_falls_back_to_static_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_js_eval, "TIMEOUT_SECONDS", 1)
    started = time.monotonic()
    script = (
        "var FLOWS = {}; FLOWS.demo = { steps: [{ say: 'Static narration still indexes.',"
        " views: [{ type: 'rows', label: 'x', items: [1].map(function () { while (true) {} }) }] }] };"
    )
    assert "Static narration still indexes." in _md(tmp_path, script)
    assert time.monotonic() - started < 6


def test_many_runaway_scripts_share_one_budget_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_js_eval, "TIMEOUT_SECONDS", 1)
    loop = "var FLOWS = {}; FLOWS.demo = { steps: [{ say: 'Narration.', views: [{ type: 'rows', items: [1].map(function () { while (true) {} }) }] }] };"
    scripts = "".join(f"<script>{loop}</script>" for _ in range(6))
    p = tmp_path / "page.html"
    p.write_text(f"<html><body><h1>Doc</h1>{scripts}</body></html>", encoding="utf-8")
    started = time.monotonic()
    list(extract(p))
    assert time.monotonic() - started < 4
