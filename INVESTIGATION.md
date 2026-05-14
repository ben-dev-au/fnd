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

Cold click-to-display (5 runs per profile). Warm is unreliable — see
"Harness notes" below.

| profile | median ms | min | max |
|---|---|---|---|
| small | 85 | 84 | 86 |
| heavy | 1134 | 499 | 1196 |
| table_heavy | 2284 | 960 | 2563 |
| fence_heavy | 348 | 306 | 377 |

Variance for heavy + table_heavy was wide (≈2.5× range). Hypothesis:
async scheduling jitter combined with widget-tree size. The L2
prototype below tightened the spread, supporting this.

## Results so far

| commit | prototype | small | heavy | table_heavy | fence_heavy |
|---|---|---|---|---|---|
| 32438f5 | baseline | 85 | 1134 | 2284 | 348 |
| 9481f51 | L2 Absolute-Hidden | 84 (-1%) | 563 (-50%) | 1352 (-41%) | 317 (-9%) |
| 2274e99 | W8 unstyled (force flat) | 15 (-82%) | 31 (-97%) | 25 (-99%) | 15 (-96%) |
| 7344e34 | W8 styled (rich.markdown) | 19 (-77%) | 114 (-90%) | 126 (-94%) | 263 (-24%) |
| 34fcb20 | F2/F3 cursor-following prefetch | — | — | — | — |

W8 unstyled is the upper bound (lose all markdown rendering). W8
styled keeps Rich's markdown formatting (headings, bold, lists, table
grid, syntax-highlighted code) at a per-fence Pygments cost.

### Did each prototype hit its exit criterion?

| # | criterion | result |
|---|---|---|
| 2a — L2 | heavy < 300 ms | **no** (563 ms, but 50% reduction) |
| 2c' — W8 styled | heavy < 200 ms AND match still lands at first hit | **yes** (114 ms, first-hit line resolution preserved) |
| 2d — F2/F3 | cursor-following extends prefetch window | shipped as code change; no separate benchmark — depends on a multi-file corpus + Pilot navigation harness that wasn't built today |

### Functional cost of W8 styled

What's lost:
- Per-block widget tree → no `link_clicked` events on inline links.
- Code fences render as styled text, not focusable / scrollable
  inner widgets.
- Tables render as ASCII grid, not focusable cells (cell-precision
  scroll becomes line-precision).
- `MarkdownTableContent` keyline / hover effects gone.

What's preserved:
- Headings, bold, italic, blockquote styling (via rich.markdown).
- Bullet / numbered lists.
- Inline code formatting.
- Match highlight spans (yellow / orange word-level).
- Scroll-to-match (line-precision via `first_hit_line_in_chunk`).
- Syntax-highlighted code (rich.syntax via rich.markdown).

### Combined ship recommendation (pending review)

For a single coherent landing:
- **W8 styled** as the default md path. Move docx/pptx through it too
  if the visual fidelity holds (untested today — they have body_md but
  the rendering may differ).
- **L2** retained for any chunks that still need the structural path
  (e.g. if we keep structural as a fallback for docx/pptx).
- **F2/F3** cursor-following prefetch — separate axis, ships with
  either.

Estimated combined cold latency on heavy md once W8 lands: ~115 ms.

### Harness notes / gotchas

- Warm-state measurements are unreliable in the current harness. The
  `_run_query` auto-load fires before the measured load, then
  prefetch races the measured load. Need a "decode the target then
  reset perf" pre-warm that avoids `_run_query` entirely.
- The "already-active scroll-only" path now has a `click_to_display_end`
  mark; it's effectively 0 ms (just a scroll).
- High variance on baseline heavy (~2.5× range) appears to be
  scheduler jitter. L2 collapses this (483-601 vs 499-1196), which is
  evidence the dominant cost was something L2 fixes (per-widget
  arrange).

### Not done today

- **W3 DataTable for markdown tables** — W8 obsoletes this for md
  files. Still potentially useful for docx/pptx if they keep the
  structural path. Deferred.
- **2b L2 refinements** (paint-while-hidden, one-frame-flip) — L2
  alone was insufficient to hit the 300 ms target. W8 styled hits a
  better target without needing 2b. Deferred.
- **gc.freeze session test** — V14 (Python 3.14 mostly fixes upstream
  GC) plus W8 eliminating most widgets means gen2 pressure is
  irrelevant once W8 ships. Deferred.

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
