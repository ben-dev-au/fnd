# Preview perf — focused investigation

## ⚠️ HANDOFF — pre-compact 2026-05-14 late evening

**Uncommitted in working tree (fnd/tui/app.py):**
- Silent-mode resume for within-file navigation. `_dispatch_preview_mount` "already active, focus not yet mounted" branch no longer calls `_show_progress_bar`; passes `silent=True` to `_mount_chunks_async`. Tests pass. NOT committed yet pending user sign-off — the user's actual concern was the underlying mounting, not the bar.

**Real outstanding bugs the user reported (NONE FIXED YET — diagnose with the harness first, no speculative edits):**

1. **Mounting happens past file ~10 even when navigating slowly.** This contradicts the cursor-following prefetch design. Expected: `_prefetch_top_results(anchor_parent_id=parent_id)` should re-anchor the prefetch window around the cursor on every settled cursor move; files entering the new window get pre-mounted ahead of click. Observed: mount work fires AT click time well past file 10, suggesting:
   - Cursor-following may not be re-anchoring as expected, OR
   - Re-anchoring happens but new files don't get pre-mounted in time (decode + mount latency exceeds nav cadence), OR
   - My `_PREFETCH_MOUNT_RADIUS = 0` change means prefetch only mounts ONE chunk; first-click then has to do Phase 1b/2 expansion. That's still on-click work, just in `_mount_chunks_async` not the cold path. Possibly that's what the user sees as "mounting".
   - **First diagnostic step:** add diag logging or a new bench that emits the prefetch window membership across nav events, plus what triggers mount work at click time. DO NOT change code before this surfaces the actual cause.

2. **Markdown formatting "weird"** — user said "it's back... weird..." after the W3 fix. Unclear what's specifically wrong. Need a screenshot or description. Possibly related to W3 DataTable visual style not matching MarkdownTable (no borders, no row wrap).

3. **Tables need row wrap + borders.** User picked **Option C** (set `Text(text, overflow="fold")` on each cell so DataTable's cell renderer wraps). **Verify** DataTable actually honours `Text.overflow` before claiming a fix. If C doesn't work, present A (CSS-only styling) and B (pre-wrap + explicit row heights) as the next options. **Do NOT swap DataTable for another widget without proposing first** — the rich.Table swap was unilateral and got reverted.

**Architectural lessons re-learned this session (now memorialised in user memory):**
- Build/use a measurement harness BEFORE writing fixes. Every speculative change in this session either failed or got reverted; every fix that landed cleanly came from a number in `bench_input_lag.py` or `auto_test.py`.
- Don't swap widget types or flip default code paths without proposing options.
- Multi-paragraph comments are an AI tell. Keep them to one line, load-bearing only.

**Current default behaviour (committed):**
- W3 DataTable for markdown tables — column-width fix landed (`_content_to_text` Content→Text conversion). Tables render with proper widths now but no borders and no row wrap.
- Pre-mount structural on by default (`_prefetch_mount_structural` runs unless `FND_NO_PREMOUNT=1`).
- `_BACKGROUND_FILL_RADIUS = 10`, `_PREFETCH_MOUNT_RADIUS = 0`.
- `_PREVIEW_CACHE_MAX_FILES = 64`.
- Wall-clock yields (`asyncio.sleep(0.002)`) in Phase 2 + prefetch loops.
- Drain stale jobs in `_prefetch_top_results`.
- Preemptive `_cancel_preview_mount_task` in `_schedule_preview_load` on cross-file cursor move.
- `_apply_pending_scroll` rebuilds strips on wrap-width mismatch (flat-path scroll race fix).
- Cold-path `_finalize_pre_reveal` polls `first_match_block` then lifts `-pre-reveal` (no retry-chain deadlock).
- `display:none` for `PreviewContainer.-hidden` (fixes PDF height=1).

**Measured behaviour (auto_test.py + bench_input_lag.py):**
- pilot.pause median: 24 ms (was 80 ms pre-W3 default)
- Cached structural scrolls/click: 1.0
- Cold path elapsed: ~150-400 ms
- PDF post-layout size: (91, 35) — full pane height
- 0 zero-region misses on cold clicks
- DOM size after 6 clicks: ~130 widgets (was 3000)

**Env flags currently usable:**
```
FND_NO_W3=1            # legacy widget-per-cell tables
FND_NO_PREMOUNT=1      # no structural pre-mount (cold path only)
FND_W_HYBRID=1         # full hybrid chunk widget (drops formatting)
FND_PREMOUNT=1         # legacy alias — now no-op as default is on (just don't set FND_NO_PREMOUNT)
FND_REVEAL_FIRST=1     # warm cache-hit reveal-first (still env-gated)
FND_FORCE_FLAT=1       # route md through flat path
FND_PREVIEW_DIAG=1     # writes /tmp/fnd-preview-diag.log
FND_PERF=1             # _perf records
```

**Harnesses:**
- `tests/perf/auto_test.py` — cold elapsed, scroll counts, flat post-layout size.
- `tests/perf/bench_input_lag.py` — pilot.pause vs asyncio.sleep(0) across phases; DOM widget count.

**Recent commits (most recent last):**
- `c099336` cold-path deadlock break + dedupe warm-path scrolls + PDF wrap guard
- `dbf1cc0` smoother tail mount + chained scroll on reveal-first
- `c5da423` force layout on pre-reveal lift + log flat-path (later rolled back inside `dab6a69`)
- `1787ced` PDF height=1 + cold-path retry chain root causes
- `db04036` test(perf): harness parses by focus_seq; remove double-fire NodeSelected
- `072b2d3` kill the journey on cold load + free the loop for input
- `9771e00` default-off structural pre-mount; new input-lag bench
- `dab6a69` first-load scroll accuracy on both structural and flat
- `bbc3001` W3 DataTable + structural pre-mount on by default
- `3d46048` W3 DataTable column widths — Content → rich.Text conversion

**Next-session priorities (the user's stated ones, in order):**
1. Diagnose why mounting happens past file ~10 with slow navigation. Build the diagnostic first — don't speculate.
2. Tables: confirm Option C (overflow="fold" on Text cells) works; if so apply; else propose A/B.
3. Clarify what "markdown formatting weird" means — screenshot needed.
4. Commit silent-mount-mode change after item 1 (it may turn out to be unnecessary if mounting itself is fixed).

---

## ✅ Progress since 2026-05-14 handoff (current state)

Defaults that ship in this branch now:

- `_PREVIEW_CACHE_MAX_FILES = 64`
- Reveal-first cache-hit path (was env-gated by `FND_REVEAL_FIRST`, still
  is — production wiring tracked under "remaining work")
- **W3 DataTable on by default** for markdown tables (`FNDMarkdownTableDT`).
  Opt out: `FND_NO_W3=1`.
- **Structural pre-mount on by default** (`_prefetch_mount_structural`).
  Opt out: `FND_NO_PREMOUNT=1`.
- `_BACKGROUND_FILL_RADIUS = 10` (was 200)
- `_PREFETCH_MOUNT_RADIUS = 0` (was implicit 7)
- `_md_hybrid.py` (W-Hybrid) stays opt-in (`FND_W_HYBRID=1`) —
  drops per-heading CSS, fence focus, link clicks. Not the default.

### Resolved issues (empirically verified via the harnesses)

| Symptom | Cause | Fix |
|---|---|---|
| Cold path 1500-1800 ms with `miss=zero-region` retries-used=30 | `_finalize_pre_reveal` ran scroll first → retry chain deadlocked while `-pre-reveal` kept regions NULL_REGION | `_do_finalize_pre_reveal` polls `first_match_block`, lifts class, schedules scroll on a two-tick chain |
| 2-3 scrolls per cached click, "loads then jumps" | Reveal-first scheduled the canonical scroll then the resume task's Phase 1a + finally re-anchor competed | `skip_internal_scrolls=True` on the resume task; finally-block re-anchor removed (inline reveal+anchor at end of Phase 2b is canonical) |
| "PDF shows a single line" | `PreviewContainer.-hidden` used `visibility:hidden + position:absolute`; hidden containers stayed in vertical flow, splitting pane height | `display:none` on `.-hidden` (verified via bench_input_lag DOM stats) |
| Cold-load shift during Phase 2b reveal | Reveal happened in batches with yields between, each batch painted the focused chunk drifting down | Reveal all + scroll-to-widget inline in one block — Textual folds both into one paint |
| Wrong position on first flat (PDF) load | `_apply_pending_scroll` race with `on_resize`: scrolled to stale visual_y when wrap_width changed | `_apply_pending_scroll` now triggers `_rebuild_strips` if wrap_width differs from current size.width before computing visual_y |
| "Key presses lag even with `q`" | Pre-mounting structural widgets balloons DOM to ~3000 widgets per session; every compositor refresh walks the tree. pilot.pause was 80 ms median | W3 DataTable on by default collapses 50 widgets per table to 1. Pause drops to 24 ms median, DOM to ~130 widgets |
| Constant lag from queued prefetch jobs after navigation | Old jobs kept draining for files the user had navigated past | `_prefetch_top_results` drains the sink queue before queueing new jobs |
| Background mount blocking input | `asyncio.sleep(0)` between mounts only yields one iteration; input pump can't drain | Real wall-clock yield (`asyncio.sleep(0.002)`) in prefetch/Phase 2 loops |
| Mount task hogged loop during arrow nav | Previous file's tail mount kept running through the debounce window | `_schedule_preview_load` preemptively cancels the in-flight mount when cursor moves to a different file |

### Measured behaviour (auto_test.py + bench_input_lag.py)

| Metric | Old (pre-investigation) | Current default |
|---|---|---|
| pilot.pause median (idle) | 80 ms | **24 ms** |
| pilot.pause p95 | 160 ms | 25 ms |
| Cold-path elapsed (auto_test, md) | ~1500 ms | ~150-400 ms |
| Cold clicks hitting zero-region | yes | 0/N |
| Cached structural avg scrolls/click | 2-3 | **1.0** |
| Flat (PDF) post-layout size | (91, 1) | (91, 35) |
| DOM widget count after 6 clicks | ~3000 | ~130 |

### W3 DataTable scroll-to-match

`FNDMarkdownTableDT.compose` now registers itself as the parent
`FNDMarkdown._first_match_block` when a matched cell exists.
`_scroll_proxy_for` detects an `FNDMarkdownTableDT` target and
calls `DataTable.move_cursor(row, column, scroll=True)` so the
matched cell scrolls into view. Cell text already carries the
match spans (baked via `_apply_highlights_after_build` on
`FNDMarkdownTH/TD` before W3 compose intercepts).

### Remaining work / open trade-offs

- **W-Hybrid is opt-in and known to drop formatting.** Per-heading CSS
  (margins, content-align, level-specific colour/background) is lost
  because text runs collapse to a single Static. Fence focus + horizontal
  scroll also lost. Workarounds documented below (link-click recovery,
  fence-focus wrapper) — none implemented yet. **Default path stays on
  W3 + legacy FNDMarkdown for headings/paragraphs/fences.**
- `_md_flat.py` (W8 styled) and `_md_hybrid.py` remain available
  behind their flags for future experimentation; not currently the
  default.
- `_PREFETCH_MOUNT_RADIUS = 0` means prefetch only mounts the
  focused chunk per file. Click expands via `_mount_chunks_async`
  Phase 1b/2. Bumping the radius re-introduces DOM bloat —
  measured tradeoff visible via `bench_input_lag.py`.
- Cold-path elapsed has variance (45 ms-400 ms in synthetic harness);
  the heavy end is per-chunk markdown widget construction cost. Not
  yet investigated whether further chunk lightening (W-Hybrid for the
  active chunk only? lazy block-widget creation?) would help.

### Env flags (current usage)

```
FND_REVEAL_FIRST=1    # warm cache-hit reveal-first (still env-gated)
FND_NO_W3=1           # opt out of W3 DataTable (back to MarkdownTH/TD)
FND_NO_PREMOUNT=1     # opt out of structural widget pre-mount
FND_W_HYBRID=1        # full hybrid chunk widget (drops formatting)
FND_W3_DATATABLE=1    # legacy alias — superseded by FND_NO_W3 (negated)
FND_FORCE_FLAT=1      # route md through the flat path
FND_PREVIEW_DIAG=1    # writes /tmp/fnd-preview-diag.log
FND_PERF=1            # writes _perf records (separate channel)
```

### Harnesses (current)

- `tests/perf/auto_test.py` — drives the app via Pilot, clicks first
  10 results, parses diag for cold elapsed / scroll counts / flat
  post-layout size. Run with `./.venv/bin/python tests/perf/auto_test.py`.
- `tests/perf/bench_input_lag.py` — measures `pilot.pause()` wall-time
  across boot/click/idle phases vs `asyncio.sleep(0)` to separate
  event-loop blocking from compositor-walk cost. Also reports DOM
  widget count.

---



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
  driven by env-gated `_perf` spans (`fnd/tui/_perf.py`).
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
  FNDChunkHybrid to post `Markdown.LinkClicked`. ~30 LOC.
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

### ⚠️ CRITICAL HANDOFF NOTES (2026-05-14 evening, before context compact)

**State of branch:** `investigation/preview-perf-2026-05-14` at commit
`e163b25`. Feature branch untouched.

**The actual fix that mattered, finally:** `_PREVIEW_CACHE_MAX_FILES`
was 8 while prefetch reaches 10+ files and F2/F3 cursor-following
extends that to 20+. Pre-mounted containers were being LRU-evicted
before the user clicked them. Every "warm" theory I had was correct
in mechanism but starved of cache entries to serve. Bumped to 64 in
commit `e163b25`. Diag now shows 11/12 clicks `cached=yes` with
`focus_in_widgets=True` and `reveal_first_env=True`.

**Remaining real-world lag (per user, after cache fix landed):**
"laggy UI very much unchanged" + "preview jumped a bit and sized,
didn't always show the match". Root causes I've identified but
NOT yet fixed:

1. **Cache-hit reveal-first kicks off `_mount_chunks_async` to fill
   partial containers in the background.** That async task takes 2-3s
   and runs on the same asyncio loop → UI lag continues even after
   the visibility flip is "instant". The flip is sub-100 ms; the
   tail mount work hogs the event loop afterwards. Fixes to consider:
   - Yield more aggressively inside `_mount_chunks_async`'s phase
     2a/2b loops (await asyncio.sleep(0) between every chunk).
   - Or fully complete the pre-mount during prefetch (no partial
     containers — bigger pre-mount cost up front, instant warm
     forever).
   - Or skip the resume entirely when cache-hit, accept the
     container as-is (incomplete-but-shown).

2. **"Preview jumped a bit and sized"** — as remaining chunks
   mount into the partial container, layout shifts, the
   user-perceived scroll position moves. Need to either:
   - Mount additional chunks BELOW the visible window first (no
     shift), then above with the existing display:none-then-reveal
     trick.
   - Or eliminate the partial-resume entirely.

3. **"Didn't always show the match"** — same retry-chain issue from
   `_do_scroll_to_chunk` waiting on `region.height > 0`. With
   cache-hit reveal-first, the container is visible immediately,
   but child regions may still be 0 for one refresh tick. Scroll
   schedule via `call_after_refresh` may fire too early.

**What's been validated empirically on real corpus:**

| claim | status |
|---|---|
| Prefetch decode + chunk_cache | ✅ firing (38 prefetch_one done in user's diag) |
| F2/F3 cursor-following | ✅ anchor changes correctly with navigation |
| Widget pre-mount in background | ✅ 21 prefetch_loop_start/_end pairs |
| Cache size adequate (was 8, now 64) | ✅ 11/12 clicks now cache_hit |
| Warm cache-hit reveal-first activates fast | ✅ flip itself is fast |
| Tail mount cost stops the UI from being snappy | 🔴 unresolved |
| First-match scroll precision after reveal-first | 🔴 unresolved |
| Layout-shift during partial-resume mount | 🔴 unresolved |

**What was claimed but turned out to be misleading:**

- "L2 50% reduction" — measured on a synthetic bench whose match
  token (`__BENCH_MATCH__`) was being parsed as markdown bold,
  so `first_match_block` never resolved and both baseline and L2
  burned the retry chain. Real-world magnitude is unverified.
- "Warm reveal-first sub-100 ms" — only measured with synthetic
  pre-mount; the path didn't actually fire in real use until the
  cache size was bumped. Now that it does fire, the *visibility
  flip* IS fast but the tail mount work negates the user-perceived
  benefit.
- "Cold reveal-first 14 ms" — the click_to_display_end mark fired
  before any content was visible. Reverted.

**Env vars currently usable:**

```
FND_REVEAL_FIRST=1     # warm cache-hit goes through visibility flip path
FND_DISABLE_L2=1       # restore original display:none CSS for A/B
FND_W_HYBRID=1         # consolidated chunk widget (text Static + DataTable + Syntax)
FND_FORCE_FLAT=1       # md routes through flat path
FND_FLAT_MD_STYLED=1   # flat path uses rich.markdown rendering
FND_W3_DATATABLE=1     # markdown table → single DataTable widget
FND_PREVIEW_DIAG=1     # writes /tmp/fnd-preview-diag.log
FND_PERF=1             # writes _perf records (separate channel)
```

**Next session priorities (in order):**

1. Verify the user-visible result of the cache-bump + FND_REVEAL_FIRST=1
   combination. The diag shows cache hits — does the FLIP itself feel
   fast even if tail mount is laggy? If yes, attack tail mount cost
   next. If no, the visibility flip + scroll path still has issues.
2. Make `_mount_chunks_async` yield aggressively so background fill
   doesn't lock up the UI. `await asyncio.sleep(0)` after every chunk
   mount in phases 2a/2b. Maybe `await asyncio.sleep(0.005)` for
   stronger yielding.
3. Fix scroll-to-match precision in the reveal-first branch — the
   `_scroll_preview_to_chunk` via call_after_refresh may fire before
   regions populate. Possible: chain two `call_after_refresh` calls
   to give layout one extra tick.
4. Consider eliminating partial pre-mount — pre-mount the WHOLE file
   during prefetch (subject to memory cap). Removes the laggy tail
   mount problem entirely. Memory cost: 64 cached files × 50-200
   chunks each × ~50 widgets per chunk = 100k-600k widgets. Probably
   too much. Compromise: pre-mount focus + larger radius (e.g. 30
   chunks instead of 7), bounded by total file size.
5. Once cold + warm both feel snappy, validate W-Hybrid on user's
   real corpus (still untested on real files).

**The user's pushback was correct throughout.** Their tests proved
the synthetic benchmark numbers were misleading and the warm path
wasn't actually firing. Every "improvement" I claimed was unverified
on real corpus until they ran the diag. Honor that going forward.

---



**Status:** prototype landed (commit `fnd/tui/app.py` + warm benchmark
results in `tests/perf/results/warm_reveal_first_v2.json`). Behind
`FND_REVEAL_FIRST=1`. Default behaviour unchanged.

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
`FND_REVEAL_FIRST=1`):

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
   cheaper. FNDChunkHybrid resolves `first_match_widget`
   synchronously at compose() time (no async build_from_token race),
   so scroll-target resolution is deterministic once region.height
   is non-zero. If pre-mount diagnosis succeeds AND W-Hybrid
   pre-mounts cleanly, this is the **no-compromise instant** answer.
2. **W-Hybrid fence-focus recovery** — wrap each Syntax in a
   `ScrollableContainer(can_focus=True)`. Adds 30 widgets to
   fence_heavy but those are simple containers, not block trees.
3. **W-Hybrid link-click wiring** — 30 LOC; restores inline link
   click handling via `action_link` on FNDChunkHybrid.
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
| `fnd/tui/_perf.py` | Env-gated timing spans. |
| `tests/perf/_corpus.py` | Synthetic corpus generator. |
| `tests/perf/bench_reveal.py` | Click-to-display benchmark runner. |
| `tests/perf/results/*.json` | Run outputs (committed for diffability). |
| `INVESTIGATION.md` | This file. |
