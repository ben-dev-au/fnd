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

| commit | prototype | small | heavy | table_heavy | fence_heavy | functional cost |
|---|---|---|---|---|---|---|
| 32438f5 | baseline | 85 | 1134 | 2284 | 348 | — |
| 9481f51 | L2 Absolute-Hidden | 84 (-1%) | 563 (-50%) | 1352 (-41%) | 317 (-9%) | **none** |
| cc032a2 | W3 DataTable (+L2) | 85 | 692 (-39%) | 505 (-78%) | 357 | minor: cell focus model changes |
| HEAD | **W-Hybrid (+L2)** | **64 (-24%)** | **272 (-76%)** | **263 (-88%)** | 399 (+15%) | medium: fence focus + per-paragraph DOM |
| 2274e99 | W8 unstyled (force flat) | 15 (-82%) | 31 (-97%) | 25 (-99%) | 15 (-96%) | very high: no rendering |
| a6e6519 | W8 styled (rich.markdown) | 19 (-77%) | 114 (-90%) | 126 (-94%) | 263 (-24%) | high: no widgets at all |
| 34fcb20 | F2/F3 cursor-following prefetch | — | — | — | — | none |

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

**Updated after W-Hybrid prototype: there's now a viable middle path
that retains most interactive functionality.**

Two coherent landing options:

**Option A — W-Hybrid + L2 + F2/F3 (functional)**
- W-Hybrid per chunk: 1 text Static + 1 DataTable per table + 1
  Syntax Static per fence. Preserves: cell-precision scroll
  (DataTable), syntax-highlighted code (Syntax), match highlights,
  scroll-to-match, link metadata (recoverable via action_link).
- L2 for any chunks that bypass W-Hybrid.
- F2/F3 cursor-following prefetch.
- Estimated heavy cold: ~270 ms. Falls short of W8's 114 ms but
  preserves what W8 throws away.

**Option B — W8 styled + L2 + F2/F3 (max-speed, max-loss)**
- W8 styled as the default md path. Loses per-cell focus, fence
  focus, all per-paragraph widget structure.
- L2 retained for non-md structural files.
- F2/F3 cursor-following prefetch.
- Estimated heavy cold: ~115 ms.

The choice between A and B is a real product decision: speed vs.
fidelity. A still hits "fast" (300 ms is well under the 2-3 s baseline
worst case the user complained about); B hits "instant" but compromises.

**Workarounds for W-Hybrid's remaining losses (Option A):**
- Inline link clicks: recoverable. Rich preserves `Style.link`
  through `render_lines` (probed today). Wire `action_link` on
  AcornChunkHybrid to post `Markdown.LinkClicked`. ~30 LOC.
- Fence focus + horizontal scroll: harder. Real `MarkdownFence`
  requires a parent Markdown widget. Workarounds: subclass to relax
  the parent requirement, or wrap Syntax in a focusable
  ScrollableContainer (one container per fence — adds widget
  back per fence, but bounded).
- Per-paragraph fine-grained scroll: lost in both A and B. Chunk-
  level scroll precision still works.

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

- **W3 DataTable standalone** — done as cc032a2. Big win on
  table_heavy (-78% vs baseline), but its real value is as a
  component inside W-Hybrid.
- **2b L2 refinements** (paint-while-hidden, one-frame-flip) — still
  unexplored. Could push L2-only path closer to a no-functional-loss
  "instant".
- **Background widget pre-build cache** — outline only. Pre-mount the
  full structural widget tree behind L2's absolute-hidden mask, so
  cold clicks are visibility flips. Equivalent functionality, max
  speed. Untested; could be the cleanest answer if implementable.
- **Smarter async mount (focused-chunk-only sync mount)** — push
  existing _mount_chunks_async further: synchronously mount JUST the
  focused chunk, lazily mount others post-reveal. Untested.
- **gc.freeze session test** — V14 (Python 3.14 mostly fixes upstream
  GC) plus widget-reduction options means gen2 pressure is less
  relevant. Lowest priority, still deferred.

### Open prototypes for tomorrow / next session

In priority order for the "fast AND functional" goal:

1. **Background widget pre-build cache** — if every prefetched file
   has its widget tree pre-mounted behind absolute-hidden, click-to-
   display is a class flip (sub-50 ms) with **zero** functional cost.
   Needs investigation of the prefetch path's mount lifecycle.
2. **W-Hybrid fence-focus recovery** — wrap each Syntax in a
   `ScrollableContainer(can_focus=True)`. Adds 30 widgets to
   fence_heavy but those are simple containers, not block trees.
3. **W-Hybrid link-click wiring** — 30 LOC; restores inline link
   click handling. Recovery is mechanically straightforward.
4. **W-Hybrid for docx/pptx** — current prototype is md-only. They
   also go through the structural path and would benefit.
5. **Smarter async mount** — synchronously mount the focused chunk
   only; lazy-mount the rest. Lowers first-paint without an
   architecture change.

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
