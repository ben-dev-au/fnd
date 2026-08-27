"""Rank tests by how often they failed across the flake-hunt matrix.

Reads the junit reports one per (OS, run) and prints a markdown summary.
A test that fails in some runs and passes in others is a flake; one that
fails in every run of an OS is a real, platform-specific failure — the
distinction the workflow exists to make.
"""

from __future__ import annotations

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


def main(root: Path) -> int:
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


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "reports")))
