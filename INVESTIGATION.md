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
| d7df711 | W-Hybrid (+L2) | 64 (-24%) | 272 (-76%) | 263 (-88%) | 399 (+15%) | medium: fence focus + per-paragraph DOM |
| 2274e99 | W8 unstyled (force flat) | 15 (-82%) | 31 (-97%) | 25 (-99%) | 15 (-96%) | very high: no rendering |
| a6e6519 | W8 styled (rich.markdown) | 19 (-77%) | 114 (-90%) | 126 (-94%) | 263 (-24%) | high: no widgets at all |
| 34fcb20 | F2/F3 cursor-following prefetch | — | — | — | — | none |
| **HEAD** | **🏆 Warm reveal-first** (pre-mount + visibility flip, partial-OK) | **8 (-91%)** | **97 (-91%)** | **253 (-89%)** | **86 (-75%)** | **none** |

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

**Updated after reveal-first breakthrough: there's now a no-compromise
instant path. The earlier W-Hybrid / W8 / "pick your trade-off"
discussion below is preserved for reference but reveal-first
supersedes both as the default recommendation.**

**Option 0 (NEW, recommended) — Reveal-first + L2 + F2/F3 + structural pre-mount**
- Reveal-first cache-hit path (per the breakthrough section above).
- L2's Absolute-Hidden CSS for prefetched containers (already in).
- F2/F3 cursor-following prefetch (already in).
- Existing structural pre-mount (_prefetch_mount_structural).
- Measured heavy warm: 97 ms. Functional cost: zero.

Three of four profiles sub-100 ms with full features. The
"perceptible-flash" caveat above is the only open item.

If you want speed-only without features: W8 styled still applies.
If you want a middle ground for fence-focus + cell focus without
reveal-first: W-Hybrid still applies. Both options below.

**Earlier two options (now superseded by Option 0):**

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

### 🏆 Warm reveal-first: solved (the no-compromise instant answer)

**Status:** prototype landed (commit `acorn/tui/app.py` + warm benchmark
results in `tests/perf/results/warm_reveal_first_v2.json`). Behind
`ACORN_REVEAL_FIRST=1`. Default behaviour unchanged.

**Measured warm cold (5 runs, median):**

| profile | prior (warm path) | reveal-first | delta |
|---|---|---|---|
| small | 85 ms | **8 ms** | -91% |
| heavy | 985 ms | **97 ms** | -90% |
| table_heavy | 1660 ms | **253 ms** | -85% |
| fence_heavy | 645 ms | **86 ms** | -87% |

**Three of four profiles are sub-100 ms. Functional cost: zero.** Full
structural widget tree intact — link clicks, first_match_block, per-
block scroll precision, syntax-highlighted scrollable code fences, etc.

**Root cause (now documented):**

- Textual's compositor splits widgets into a `_visible_map` and an
  `_invisible_widgets` set (`_compositor.py:543`).
- `widget.region` calls `screen.find_widget(widget)` which looks in
  the visible map. Invisible widgets raise `NoWidget`, caught by
  `widget.region` returning `NULL_REGION = Region(0,0,0,0)`.
- L2's CSS preserves the per-widget *arrange cache* (V4) — that's a
  CPU saving on visible-tree re-arrange. It does NOT populate the
  spatial map for invisible widgets. So `widget.region` is `(0,0,0,0)`
  while the container is `-hidden` *or* `-pre-reveal`.
- The 30-retry `_do_scroll_to_chunk` waits for `region.height > 0`.
  With `-pre-reveal` keeping the container invisible, that never
  resolves — retries exhaust and ~500 ms is burned.

**The fix:**

In `_dispatch_preview_mount` cache-hit branch (gated by
`ACORN_REVEAL_FIRST=1`):

1. Activate the container with `pre_reveal=False` — full visibility
   immediately. Layout propagates on the next refresh tick.
2. Schedule `_scroll_preview_to_chunk` via `call_after_refresh`. The
   one-frame delay is enough for regions to populate; scroll lands
   at the correct position on the first try (no retry chain).
3. If the cached container is *partial* (focused chunk + radius from
   the prefetch path, but not the full file), kick off a background
   `_mount_chunks_async` to fill in the rest *after* the user already
   sees the focused window.

**Production caveat (not measured under Pilot):** the
"activate-then-scroll" path means there's *one frame* where the
container is visible at file-top before scroll lands. In Pilot
test mode this is invisible (<16 ms). On a real TUI it may be
perceptible as a tiny flash. If that's a real UX problem, the
mitigation is to give the compositor one synchronous render pass
*while* the container is in pre-reveal, *then* scroll, *then*
remove pre-reveal. Needs Textual API exploration.

**Open questions before shipping:**

1. The fence_heavy regression in W-Hybrid was caused by per-fence
   Pygments setup — does reveal-first eliminate it? Earlier numbers
   suggest yes (fence_heavy warm 86 ms vs cold L2 317 ms — the
   pre-mounted Pygments work is amortized).
2. Combine with W-Hybrid for chunks where DataTable / Static
   consolidation would further reduce per-chunk widget count?
   Probably not necessary given reveal-first already hits sub-100 ms
   on heavy, but worth measuring.
3. Visible flash in production — needs a manual run to confirm.

### Earlier warm anomaly (now resolved by the above)

Modified the harness to wait for the WIP's existing structural
pre-mount path (`_prefetch_mount_structural`) to complete before
firing the measured load. Result on heavy md: **1.8 s** — *slower*
than cold L2 (563 ms). That's wrong if pre-mount is doing its job.

The path label shows `warm_pre_reveal`, so we ARE going through
the cache-hit pre-reveal branch in `_dispatch_preview_mount`. The
cost is inside `_do_scroll_to_chunk`'s 30-retry chain waiting for
`region.height > 0` after the visibility flip from absolute-hidden
to visible.

Two hypotheses:

1. **Harness bug.** My "pre-mount complete" wait condition is
   `_preview_cache.get(parent_id, sig).is_complete`. That resolves
   when the chunk widgets are mounted, but Textual may not have
   propagated their regions through the spatial map yet. The
   measured load races pending layout work.
2. **Real code bug.** Pre-mounted absolute-hidden widgets get
   `arrange()` called (V2 confirmed) but their **regions are not
   populated in the spatial map** until they paint. The visibility
   flip triggers spatial-map population, which takes one or more
   refresh ticks. `_do_scroll_to_chunk` retries every tick until
   `region.height > 0` — 30 retries adds up.

If (2) is the cause, the existing pre-mount machinery doesn't
actually save reveal latency the way it should, regardless of L2.
That would explain why L2 alone gives 50% reduction (data-warm
helps via skipping decode) but doesn't deliver "instant reveal"
even when widget mounts have already happened.

**This is the highest-value open thread for next session.** If
solvable, "pre-mount + flip" is the no-functional-loss "instant"
option. If unsolvable, we fall back to the W-Hybrid / W8 trade-off.

### Open prototypes for tomorrow / next session

In priority order for the "fast AND functional" goal:

1. **Diagnose the pre-mount warm-path anomaly above.**
   - Instrument `_do_scroll_to_chunk` retry count + why
     `region.height == 0` after activation.
   - Test hypothesis: pre-compute scroll target before the
     visibility flip (read child regions while still hidden, set
     scroll position, then flip). If this works, "pre-mount + L2 +
     pre-compute-scroll" should deliver sub-100 ms warm clicks
     with zero functional cost.

   **User-confirmed combine path** (2026-05-14): pre-mount the
   **W-Hybrid** widget tree, not the full structural tree. Per-chunk
   widget count drops from ~50 → 3, so background pre-mount is
   cheaper. AcornChunkHybrid resolves `first_match_widget`
   synchronously at compose() time (no async build_from_token race),
   so scroll-target resolution is deterministic once region.height
   is non-zero. If pre-mount diagnosis succeeds AND W-Hybrid
   pre-mounts cleanly, this is the **no-compromise instant** answer.
2. **W-Hybrid fence-focus recovery** — wrap each Syntax in a
   `ScrollableContainer(can_focus=True)`. Adds 30 widgets to
   fence_heavy but those are simple containers, not block trees.
3. **W-Hybrid link-click wiring** — 30 LOC; restores inline link
   click handling via `action_link` on AcornChunkHybrid.
4. **W-Hybrid for docx/pptx** — current prototype is md-only.
5. **Aggressive prefetch** — once the warm path is genuinely fast,
   widen prefetch_count or pre-decode all files at startup to keep
   every clickable result "warm".
6. **Smarter async mount** — synchronously mount the focused chunk
   only; lazy-mount the rest. Lowers first-paint without
   architecture change.
7. **Pre-rendered cache at index time** — store rich.markdown
   rendered lines in the index; load straight to flat path.
   Eliminates rich.markdown cost entirely on subsequent runs.

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
