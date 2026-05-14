# Preview perf — focused investigation

Scope is tight on purpose: validate every option that could close the
gap between current click-to-display latency and "feels instant",
without rebuilding what already works.

## Constraint

All experiments live in this worktree (`.worktrees/perf-investigation/`)
on branch `investigation/preview-perf-2026-05-14`. The feature branch
`feat/settings-menu-redesign-p3` must remain untouched.

## In scope

- Regression fix for S1/S2 (preview doesn't show / lands at file top).
- Layout-cost reduction (Absolute-Hidden + refinements).
- Widget-count reduction:
  - W3 — DataTable for markdown tables.
  - W8 — flat path (`LineBufferPreview`) for whole md files.
- Cursor-following prefetch (F2/F3 of yesterday's plan).
- GC session experiment.

## Out of scope (explicitly)

- L6/W7 JIT block virtualization (vendoring 0x7c13's Textual fork or
  building equivalent from scratch). User direction: too intensive.
- Architectural Rich chunk-rendering (W6). Previously implemented and
  removed.
- Re-parenting / staging containers. R-Lay confirms wasteful.

## Method

- Synthetic corpus at four profiles (`tests/perf/_corpus.py`): small,
  heavy, table_heavy, fence_heavy.
- Click-to-display measured via Pilot (`tests/perf/bench_reveal.py`),
  driven by env-gated `_perf` spans (`acorn/tui/_perf.py`).
- Each option = one commit on the investigation branch. Diff between
  commits = the change being measured. Baseline result lives in
  `tests/perf/results/`.
- 5 runs per (profile, warm-state) pair; report median + p95.

## Baseline (commit 32438f5 — feature-branch WIP imported)

See `tests/perf/results/baseline_v1.json`. Filled in once the run
completes.

| profile | warm | median ms | p95 ms |
|---|---|---|---|
| small | cold | _pending_ | _pending_ |
| heavy | cold | _pending_ | _pending_ |
| table_heavy | cold | _pending_ | _pending_ |
| fence_heavy | cold | _pending_ | _pending_ |
| ... | warm | _pending_ | _pending_ |

## Exit criteria per prototype

| # | Prototype | "Ship-worthy" threshold |
|---|---|---|
| 2a | L2 Absolute-Hidden basic | heavy cold median < 300 ms (vs baseline) |
| 2b | L2 refinements (paint-while-hidden, one-frame flip) | only if 2a < 300 ms but ≥ 150 ms; then push toward < 100 ms |
| 2c | W3 DataTable for tables | table_heavy widget-count reduced ≥ 50%; cell-precision scroll still lands at the matched cell |
| 2c' | W8 flat-path-for-md | heavy cold median < 200 ms AND match lands at first hit AND highlights render |
| 2d | Cursor-following prefetch | held-arrow median latency < 150 ms; no flood on rapid arrow |
| 2e | gc.freeze | median per-mount latency reduction ≥ 30 ms across a 30-min Pilot run |

Anything that doesn't hit its threshold: documented, abandoned.

## Synthesis (Phase 3)

After all prototypes, pick a coherent ship set. Trade-offs:

- W3 and W8 both reduce widgets; W8 obsoletes W3 (no widgets to
  reduce). Pick one.
- L2 and W8 compose; L2 and W3 compose.
- If W8 is below threshold AND L2 is below threshold, F2/F3 + gc.freeze
  + W3 may be enough as combined mitigation. Otherwise, deeper bets.

## Files

| Path | Purpose |
|---|---|
| `acorn/tui/_perf.py` | Env-gated timing spans. |
| `tests/perf/_corpus.py` | Synthetic corpus generator. |
| `tests/perf/bench_reveal.py` | Click-to-display benchmark runner. |
| `tests/perf/results/*.json` | Run outputs (committed for diffability). |
| `INVESTIGATION.md` | This file. |
