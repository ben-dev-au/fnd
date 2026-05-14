"""Synthetic corpus generator for preview-perf benchmarks.

Produces md files at three complexity tiers. Every file embeds the
string ``__BENCH_MATCH__`` exactly once, at a known logical position,
so the benchmark can fire a deterministic query and measure
match-resolution latency without relying on Tantivy ranking ordering
between runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MATCH_TOKEN = "__BENCH_MATCH__"

Profile = Literal["small", "heavy", "table_heavy", "fence_heavy"]


@dataclass(frozen=True)
class CorpusSpec:
    profile: Profile
    headings: int
    paragraphs_per_heading: int
    table_count: int
    table_rows: int
    table_cols: int
    fence_count: int
    fence_lines: int
    match_at_block: int


SMALL = CorpusSpec(
    profile="small",
    headings=3,
    paragraphs_per_heading=4,
    table_count=0,
    table_rows=0,
    table_cols=0,
    fence_count=0,
    fence_lines=0,
    match_at_block=5,
)

HEAVY = CorpusSpec(
    profile="heavy",
    headings=30,
    paragraphs_per_heading=8,
    table_count=4,
    table_rows=12,
    table_cols=4,
    fence_count=6,
    fence_lines=20,
    match_at_block=180,
)

TABLE_HEAVY = CorpusSpec(
    profile="table_heavy",
    headings=8,
    paragraphs_per_heading=3,
    table_count=20,
    table_rows=20,
    table_cols=5,
    fence_count=0,
    fence_lines=0,
    match_at_block=120,
)

FENCE_HEAVY = CorpusSpec(
    profile="fence_heavy",
    headings=8,
    paragraphs_per_heading=2,
    table_count=0,
    table_rows=0,
    table_cols=0,
    fence_count=30,
    fence_lines=25,
    match_at_block=150,
)

PROFILES: dict[Profile, CorpusSpec] = {
    "small": SMALL,
    "heavy": HEAVY,
    "table_heavy": TABLE_HEAVY,
    "fence_heavy": FENCE_HEAVY,
}


def _para(i: int) -> str:
    return (
        f"Paragraph {i} body content with several words of filler text, "
        f"enough to wrap across multiple terminal columns and to exercise "
        f"the inline-style code path within MarkdownParagraph."
    )


def _heading(level: int, idx: int) -> str:
    return f"{'#' * level} Section {idx} heading"


def _table(rows: int, cols: int, idx: int, embed_match: bool = False) -> list[str]:
    out: list[str] = []
    header = ["col-" + str(c) for c in range(cols)]
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join(["---"] * cols) + " |")
    for r in range(rows):
        cells = [f"t{idx}r{r}c{c}" for c in range(cols)]
        if embed_match and r == rows // 2:
            cells[cols // 2] = MATCH_TOKEN
        out.append("| " + " | ".join(cells) + " |")
    return out


def _fence(lines: int, idx: int, embed_match: bool = False) -> list[str]:
    out: list[str] = ["```python"]
    for ln in range(lines):
        if embed_match and ln == lines // 2:
            out.append(f"# {MATCH_TOKEN} marker on line {ln}")
        else:
            out.append(f"def fn_{idx}_{ln}(x):  # filler comment {ln}")
            out.append(f"    return x + {ln}")
    out.append("```")
    return out


def render(spec: CorpusSpec) -> str:
    """Render an md file matching ``spec``. ``MATCH_TOKEN`` appears
    exactly once, in the block at logical index ``spec.match_at_block``
    (counting paragraphs/tables/fences in order).
    """
    out: list[str] = [f"# Synthetic corpus — {spec.profile}", ""]
    block_index = 0
    placed_match = False
    for h in range(spec.headings):
        out.append(_heading(2, h))
        out.append("")
        for p in range(spec.paragraphs_per_heading):
            if block_index == spec.match_at_block and not placed_match:
                out.append(_para(block_index) + " " + MATCH_TOKEN)
                placed_match = True
            else:
                out.append(_para(block_index))
            out.append("")
            block_index += 1
    for t in range(spec.table_count):
        embed = (
            spec.profile == "table_heavy"
            and t == spec.table_count // 2
            and not placed_match
        )
        out.extend(_table(spec.table_rows, spec.table_cols, t, embed_match=embed))
        out.append("")
        if embed:
            placed_match = True
        block_index += 1
    for f in range(spec.fence_count):
        embed = (
            spec.profile == "fence_heavy"
            and f == spec.fence_count // 2
            and not placed_match
        )
        out.extend(_fence(spec.fence_lines, f, embed_match=embed))
        out.append("")
        if embed:
            placed_match = True
        block_index += 1
    if not placed_match:
        out.append(f"Trailing {MATCH_TOKEN} fallback so the query always hits.")
    return "\n".join(out) + "\n"


def write_corpus(root: Path, specs: list[CorpusSpec]) -> dict[Profile, Path]:
    """Write each spec into ``root/{profile}.md``. Returns the path map."""
    root.mkdir(parents=True, exist_ok=True)
    out: dict[Profile, Path] = {}
    for spec in specs:
        path = root / f"{spec.profile}.md"
        path.write_text(render(spec), encoding="utf-8")
        out[spec.profile] = path
    return out
