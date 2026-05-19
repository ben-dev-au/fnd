#!/usr/bin/env python3
"""Phase 0 spike — measure reliability of Preview page-jump via AppleScript.

Will be deleted before the feature branch merges. Decides Gate A:

  ≥ 9 / 10 trials succeed AND p95 latency < 2.0s  →  Preview becomes the
    auto-default PDF app in the apps registry.
  Otherwise  →  Skim regains auto-default-when-present; Preview stays in the
    registry as a best-effort no-page-jump entry.

WARNING — running this opens Preview, brings it to the foreground, and types
keystrokes 10+ times. Do NOT run it while typing into another app. Pick a
calm minute, run, walk away, read the report.

Usage::

    .venv/bin/python scripts/spike_preview_page_jump.py
    .venv/bin/python scripts/spike_preview_page_jump.py --pdf path.pdf --trials 20
    .venv/bin/python scripts/spike_preview_page_jump.py --quick   # 3 trials only

The script verifies the page jump by reading Preview's current page back via
``tell document 1 of application "Preview" to get current page``. A mismatch
is counted as a failure even when osascript itself returns 0.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Script kept verbatim with the production handler in fnd/apps.py so any tuning
# learnt here transfers without translation.
PAGE_JUMP_SCRIPT = r"""
on run argv
    set pdfPath to item 1 of argv
    set pageNum to item 2 of argv
    tell application "Preview"
        activate
        open POSIX file pdfPath
    end tell
    -- Poll for the doc to become front, up to 3s.
    set tries to 0
    repeat until tries > 30
        try
            tell application "Preview"
                if (exists front document) and (path of front document contains pdfPath) then exit repeat
            end tell
        end try
        delay 0.1
        set tries to tries + 1
    end repeat
    tell application "Preview" to activate
    delay 0.1
    tell application "System Events"
        tell process "Preview"
            keystroke "g" using {option down, command down}
            delay 0.15
            keystroke pageNum
            delay 0.05
            key code 36
        end tell
    end tell
end run
"""

READ_PAGE_SCRIPT = (
    'tell application "Preview" to if exists front document then '
    "return current page of front document as integer"
)
QUIT_SCRIPT = 'tell application "Preview" to quit saving no'


@dataclass
class Trial:
    page: int
    scenario: str
    success: bool
    elapsed: float
    reported_page: int | None
    error: str = ""


def run_osascript(args: list[str], *, timeout: float = 12.0) -> tuple[int, str, str]:
    proc = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def quit_preview() -> None:
    run_osascript(["osascript", "-e", QUIT_SCRIPT])
    time.sleep(0.4)


def ax_check() -> str | None:
    """Returns None if Accessibility (keystroke) permission is granted to
    the calling process; else a human-readable reason string.

    Sends an empty keystroke to Finder — a benign operation that exercises
    the same AX gate as the page-jump script. ``-1719`` and ``-25211``
    are the standard "not authorized" / "assistive access denied" codes.
    Reading process names (cheaper probe) goes through *Automation*
    permission instead of *Accessibility*, so it can pass while the real
    keystroke fails. Don't downgrade this probe.
    """
    rc, out, err = run_osascript(
        [
            "osascript",
            "-e",
            (
                "try\n"
                '  tell application "System Events" to tell process "Finder" to keystroke ""\n'
                '  return "ax_ok"\n'
                "on error errMsg number errNum\n"
                '  return "ax_error " & errNum & " " & errMsg\n'
                "end try"
            ),
        ]
    )
    if rc != 0:
        return f"osascript probe failed (rc={rc}): {err}"
    if out.startswith("ax_error"):
        return f"Accessibility access denied: {out}"
    return None


def read_current_page() -> int | None:
    rc, out, _ = run_osascript(["osascript", "-e", READ_PAGE_SCRIPT])
    if rc != 0 or not out.strip():
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def run_trial(pdf: Path, page: int, scenario: str) -> Trial:
    if scenario == "cold":
        quit_preview()
    elif scenario == "warm-same":
        pass  # leave Preview where it is
    elif scenario == "warm-different":
        # Open a different file in Preview first, then run the target.
        # Pick the same PDF but at page 1 to put it in a known state.
        run_osascript(
            [
                "osascript",
                "-e",
                PAGE_JUMP_SCRIPT,
                str(pdf),
                "1",
            ]
        )
        time.sleep(0.6)

    start = time.perf_counter()
    rc, _, err = run_osascript(
        ["osascript", "-e", PAGE_JUMP_SCRIPT, str(pdf), str(page)],
        timeout=15.0,
    )
    elapsed = time.perf_counter() - start

    if rc != 0:
        return Trial(page, scenario, False, elapsed, None, err or f"rc={rc}")

    # Give Preview a beat to commit the page change before we read it back.
    time.sleep(0.4)
    actual = read_current_page()
    if actual is None:
        return Trial(page, scenario, False, elapsed, None, "read-back failed")
    if actual != page:
        return Trial(
            page, scenario, False, elapsed, actual, f"page mismatch: wanted {page}, got {actual}"
        )
    return Trial(page, scenario, True, elapsed, actual)


def report(trials: list[Trial]) -> int:
    passed = sum(1 for t in trials if t.success)
    total = len(trials)
    elapsed = [t.elapsed for t in trials if t.success]
    p95 = statistics.quantiles(elapsed, n=20)[18] if len(elapsed) >= 5 else max(elapsed, default=0)
    mean = statistics.mean(elapsed) if elapsed else 0

    print()
    print("=" * 72)
    print(f"Preview page-jump spike: {passed}/{total} succeeded")
    if elapsed:
        print(f"Latency (successful trials): mean {mean:.2f}s, p95 {p95:.2f}s")
    print("=" * 72)
    for t in trials:
        flag = "OK " if t.success else "FAIL"
        extra = f" ({t.error})" if t.error else ""
        got = f" got_page={t.reported_page}" if t.reported_page else ""
        print(f"  [{flag}] {t.scenario:15s} page={t.page:>3} {t.elapsed:5.2f}s{got}{extra}")

    gate = passed / total >= 0.9 and p95 < 2.0
    print()
    if gate:
        print(">>> GATE PASSED: Preview can ship as the PDF default.")
        return 0
    print(">>> GATE FAILED: keep Skim as auto-default-when-present; Preview")
    print("    stays a registry entry without page-jump.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path("tests/fixtures/papers/test.pdf"),
        help="Multi-page PDF to test against (default: tests/fixtures/papers/test.pdf, 12 pages).",
    )
    parser.add_argument("--trials", type=int, default=10, help="Number of trials (default 10).")
    parser.add_argument(
        "--quick", action="store_true", help="3 trials only (smoke test before the full run)."
    )
    args = parser.parse_args(argv)

    pdf = args.pdf.expanduser().resolve()
    if not pdf.exists():
        print(f"PDF not found: {pdf}", file=sys.stderr)
        return 2

    if sys.platform != "darwin":
        print("Spike is macOS-only (uses osascript + Preview).", file=sys.stderr)
        return 2

    ax_err = ax_check()
    if ax_err:
        print()
        print("✗ Accessibility permission not granted to whatever launched this script.")
        print(f"  Reason: {ax_err}")
        print("  Grant in System Settings → Privacy & Security → Accessibility, then retry.")
        print(
            "  (The spike requires keystroke automation; this also gates the production handler.)"
        )
        return 3

    trials_n = 3 if args.quick else args.trials
    print(f"Running {trials_n} trials against {pdf} (12 pages).")
    print("Stay out of the keyboard while the spike runs.")
    print()

    # Mix scenarios across the trial count: roughly half cold-launch, the rest
    # warm transitions. Pages chosen to force movement (never stay on the page
    # Preview opened to by default).
    scenarios = ["cold", "warm-different", "warm-same"]
    pages_cycle = [2, 5, 8, 11, 3, 9, 6, 4, 7, 10]
    trials: list[Trial] = []
    for i in range(trials_n):
        page = pages_cycle[i % len(pages_cycle)]
        scenario = scenarios[i % len(scenarios)]
        print(
            f"[{i + 1:>2}/{trials_n}] scenario={scenario:<15s} page={page} ...", end=" ", flush=True
        )
        t = run_trial(pdf, page, scenario)
        trials.append(t)
        print("OK" if t.success else f"FAIL ({t.error})")
        time.sleep(0.6)

    return report(trials)


if __name__ == "__main__":
    raise SystemExit(main())
