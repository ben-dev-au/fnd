"""Rank tests by how often they failed across the flake-hunt matrix.

Reads the junit reports one per (OS, run) and prints a markdown summary.
A test that fails in some runs and passes in others is a flake; one that
fails in every run of an OS is a real, platform-specific failure — the
distinction the workflow exists to make.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree

_DIR = re.compile(r"^junit-(?P<os>.+)-(?P<index>\d+)$")


def _outcome(case: ElementTree.Element) -> str:
    for child in case:
        tag = child.tag.lower()
        if "rerun" in tag:
            return "rerun"
        if "failure" in tag or "error" in tag:
            return "fail"
        if "skipped" in tag:
            return "skip"
    return "pass"


def _detail(case: ElementTree.Element) -> str:
    for child in case:
        tag = child.tag.lower()
        if "failure" in tag or "error" in tag:
            text = (child.get("message") or child.text or "").strip()
            return " ".join(text.split())[:200]
    return ""


def main(root: Path, expect_oses: list[str] | None = None, expect_runs: int = 0) -> int:
    runs: dict[str, set[str]] = defaultdict(set)
    fails: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    reruns: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    detail: dict[str, str] = {}
    missing: list[str] = []

    for report_dir in sorted(root.glob("junit-*")):
        matched = _DIR.match(report_dir.name)
        if not matched:
            continue
        os_name, index = matched["os"], matched["index"]
        runs[os_name].add(index)
        report = report_dir / "junit.xml"
        if not report.exists():
            missing.append(f"{os_name} #{index}")
            continue
        try:
            # The input is pytest's own report from the job that produced
            # this artifact, not user-supplied XML.
            tree = ElementTree.parse(report)  # noqa: S314
        except ElementTree.ParseError:
            missing.append(f"{os_name} #{index} (unparseable)")
            continue
        for case in tree.iter("testcase"):
            nodeid = f"{case.get('classname', '')}::{case.get('name', '')}"
            outcome = _outcome(case)
            if outcome == "fail":
                fails[nodeid][os_name].add(index)
                detail.setdefault(nodeid, _detail(case))
            elif outcome == "rerun":
                reruns[nodeid][os_name].add(index)

    total = sum(len(v) for v in runs.values())
    out = [f"## Flake hunt — {total} suite runs\n"]

    # A job that dies before uploading leaves no directory at all, so a smaller
    # count is the ONLY trace of it. Say so loudly: a table that silently
    # describes fewer runs than were asked for reads as a clean result.
    if expect_oses and expect_runs:
        absent = [
            f"{o} #{i}"
            for o in expect_oses
            for i in range(1, expect_runs + 1)
            if str(i) not in runs.get(o, set())
        ]
        if absent:
            out.append(
                f"> **{len(absent)} of {len(expect_oses) * expect_runs} suites reported "
                f"nothing** — {', '.join(absent)}. Still running, or the job died before "
                "uploading. Everything below describes only the suites that reported.\n"
            )
    out.append("| OS | runs | runs with a failure |")
    out.append("| --- | ---: | ---: |")
    for os_name in sorted(runs):
        red = {i for t in fails.values() for i in t.get(os_name, set())}
        out.append(f"| {os_name} | {len(runs[os_name])} | {len(red)} |")
    out.append("")

    if missing:
        out.append(f"> No report from: {', '.join(missing)} — job died before pytest wrote one.\n")

    if not fails and not reruns:
        out.append("**Every run green.** No test failed in any run.")
        print("\n".join(out))
        return 0

    out.append("### Tests by failure count\n")
    out.append("| test | fails | of | OSes | first message |")
    out.append("| --- | ---: | ---: | --- | --- |")
    ranked = sorted(fails.items(), key=lambda kv: -sum(len(v) for v in kv[1].values()))
    for nodeid, by_os in ranked:
        count = sum(len(v) for v in by_os.values())
        possible = sum(len(runs[os_name]) for os_name in by_os)
        spread = ", ".join(f"{o}({len(by_os[o])}/{len(runs[o])})" for o in sorted(by_os))
        out.append(f"| `{nodeid}` | {count} | {possible} | {spread} | {detail.get(nodeid, '')} |")
    out.append("")
    out.append(
        "A test red in *every* run of an OS is a real platform failure, not a flake. "
        "One red in some runs and green in others is the flake list, ranked."
    )

    if reruns:
        out.append("\n### Passed only on a rerun (`@pytest.mark.flaky`)\n")
        out.append("| test | reruns | OSes |")
        out.append("| --- | ---: | --- |")
        for nodeid, by_os in sorted(
            reruns.items(), key=lambda kv: -sum(len(v) for v in kv[1].values())
        ):
            count = sum(len(v) for v in by_os.values())
            out.append(f"| `{nodeid}` | {count} | {', '.join(sorted(by_os))} |")
        out.append("\nThese are flakes that CI currently hides.")

    print("\n".join(out))
    return 0


def _json_list(arg: str) -> list[str]:
    """A GitHub matrix output, tolerating the bare comma-separated form too."""
    try:
        return [str(v) for v in json.loads(arg)]
    except (ValueError, TypeError):
        return [v.strip() for v in arg.strip("[]").replace('"', "").split(",") if v.strip()]


if __name__ == "__main__":
    # argv: <reports-dir> [oses-json] [indices-json] — the matrix that was ASKED
    # for, so a suite that never reported is named rather than silently missing.
    _dir = Path(sys.argv[1] if len(sys.argv) > 1 else "reports")
    _oses = _json_list(sys.argv[2]) if len(sys.argv) > 2 else None
    _runs = len(_json_list(sys.argv[3])) if len(sys.argv) > 3 else 0
    raise SystemExit(main(_dir, _oses, _runs))
