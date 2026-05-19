# Preview pane — DOM architecture plan

> Note: env vars referenced below were renamed `FND_*` → `_FND_*` on 2026-05-19
> (private-knob convention). Older snapshots in this doc may show the old names.


**Status.** Source of truth for the structural-preview DOM rework.
Supersedes `INVESTIGATION.md` and `docs/archive/RESEARCH_VS_CURRENT.md`.
**Branch.** Implementation will start on a fresh branch off `main`; the
investigation branch (`investigation/preview-perf-2026-05-14`) is
preserved for history and tagged at its tip.

This plan consolidates:

- `preview-dom-analysis.md` (the structured comparison of three AI
  proposals against `fnd`'s current code) — **primary source**.
- The three original AI responses (Gemini, GPT 5.4, Claude 4.7) in
  `# GPT & Gemini Responses - DOM.md`.
- Empirical measurements taken on this branch via
  `tests/perf/bench_user_symptoms.py` and
  `tests/perf/bench_input_lag.py`.
- Lessons from the
  [propose-before-arch-changes](../.claude/projects/-Users-BenDavidson-Documents-Programming-Projects-Search-Tool/memory/feedback_propose_before_arch_changes.md)
  and
  [measure-then-implement](../.claude/projects/-Users-BenDavidson-Documents-Programming-Projects-Search-Tool/memory/feedback_measure_then_implement.md)
  memory files.

---

## TL;DR

1. **The problem is the structural pipeline only.** The flat (PDF /
   TXT) pipeline already uses the single-widget-per-file
   `LineBufferPreview` pattern the research recommends. Markdown /
   docx / pptx still mount one `FNDMarkdown` widget per chunk and
   that tree is what blows up the DOM, dominates refresh ticks, and
   pushes click latency from synthetic ~0.85 s on the harness to a
   user-reported 3–6 s on real corpora.
2. **`display: none` does not exempt a widget from the compositor
   walk.** Empirically measured on this branch.
3. **Three AI proposals converge on the same diagnosis.** Cache
   *rendered output*, not widget instances. One carrier widget +
   overlay layers beats per-block widget trees. The disagreement is
   ordering, not destination.
4. **The lowest-risk first move is screen-per-file LRU.** Textual's
   `App._screen_stacks` keeps suspended screens alive in Python but
   excludes them from the active screen's compositor walk. If that
   claim holds when measured, the cache problem dissolves with no
   rendering changes. Stage 0 verifies the claim in ~1 hour.
5. **The plan is staged**, not "pick one pattern". Pure-refactor
   foundations (Stage 1: `RenderedDocument`, Stage 2: off-thread
   encoding) precede the screen-per-file spike (Stage 3). Two-tier
   (Stage 4) and full strip-list-plus-islands (Stage 5) are
   conditional fallbacks.

---

## Empirical baseline (as of branch tip)

`tests/perf/bench_input_lag.py` against a synthetic 5-file heavy-md
corpus on branch `investigation/preview-perf-2026-05-14` at HEAD:

| Metric | Pre-W3 (~Aug 14) | W3 baseline | Current branch |
|---|---|---|---|
| DOM (preview pane) | ~3000 widgets | ~130 | **793 widgets / 5 files cached** |
| pilot.pause median (idle) | 80 ms | 24 ms | **44 ms** |
| pilot.pause p95 during click | n/a | <50 ms | **1083 ms** |
| pilot.pause max | n/a | <50 ms | **1124 ms** |
| Symptom-harness steady-state click | n/a | n/a | **~0.85 s synthetic** |
| User-reported real-corpus click | n/a | n/a | **3–6 s** |

Extrapolated: at `LRU_CAP = 16` fully populated with heavy md, the
current code would hold ~2500 widgets — back in the unresponsive zone.

These are the numbers Stage 3's decision gate will compare against.

---

## Hard constraints — implementation MUST preserve all of these

**Functional (1–11)**

1. Scroll to a specific chunk and to the matched line within that
   chunk.
2. Per-line / per-character match highlights with three style
   variants: exact-literal, fuzzy (highlighted char by char where
   alignment differs), focused-chunk band.
3. Visible separator / gap between chunks.
4. Match-position tick markers on the scrollbar, accurate to true
   line position. Currently **line-precise on flat path,
   chunk-uniform on structural path** — should converge to line-
   precise.
5. Sidebar's "page N of M" / chunk metadata — preview-renderer-
   independent.
6. Cross-file LRU cache so revisits are instant (no re-decode, no
   re-render).
7. Cursor-following prefetch buffer — next N files pre-decoded and
   pre-rendered ahead of navigation.
8. Multi-line text selection and clipboard copy from the preview.
9. Markdown semantic rendering: headings (per-level styling),
   paragraphs, ordered / unordered lists, blockquotes, inline code,
   inline emphasis, **fenced code blocks with syntax highlighting**
   (`rich.syntax.Syntax`), **tables** (currently as `DataTable` per
   markdown table so wide tables scroll), and reasonable fallbacks
   for links.
10. Live query re-runs while a preview is open must update highlights
    without re-decoding or re-rendering the document — only the
    match spans change.
11. Reasonable performance on documents up to ~1000 pages
    (PDF/DOCX/PPTX text layer) and up to ~100 k lines for plain text.

**Performance (A–D)**

- **A.** Steady-state cache-hit click latency <100 ms perceived.
- **B.** `pilot.pause()` median <25 ms, max <50 ms.
- **C.** LRU cache must not cause DOM widget count to scale with
  cache size in a way that breaks (B).
- **D.** Preview pre-mount must not block the event loop or starve
  keystroke handling.

---

## Current state — what's already shipped

These commits on the investigation branch are confirmed-good and
should be preserved when the implementation branch starts fresh.

| Commit | Why keep |
|---|---|
| `bbc3001` "W3 DataTable + structural pre-mount default-on" | Tables now render as one `DataTable` per markdown table; major DOM reduction even without further work. |
| `3d46048` "W3 DataTable column widths" | Bug fix — columns were 1-char wide. |
| `dab6a69` "first-load scroll accuracy" | Removed a competing chained re-anchor that caused "lands then jumps". |
| `1787ced` "PDF height=1 + cold-path retry chain" | Established the retry-chain framework for `_finalize_pre_reveal`. The polling itself is being replaced (see `0850012`) but the diagnostic + tests live on. |
| `b24a4be` "silent within-file resume" | Hides progress bar on same-file next-match scroll. |
| `ba14fb3` "user-symptom harness; title-refresh fix" | **Mixed.** Keep: title fix (in `_activate_preview_container`), diag timestamps, `bench_user_symptoms.py`, `bench_prefetch_window.py`, leak-probe fixture bump. **Roll forward**: the LRU=64→16 and removal of `_BACKGROUND_FILL_RADIUS` / `_PREFETCH_MOUNT_RADIUS` were experimental and the data showed they made things worse — Stage 0a below restores them. |
| `0850012` "cold-mount reveal awaits md_widget.lock" | Real fix for "first load not showing match" on heavy md. Replaces the polling retry chain with event-based wait. |

The fix-up commit below (Stage 0a) is a forward-only restoration; the
experimental state is preserved in history at `ba14fb3`.

---

## Pattern analysis — consolidated

Each row is the same pattern under whatever name each response used.
See `preview-dom-analysis.md` for the full code-anchored discussion;
this table is the durable summary.

| Pattern (canonical name) | Aliases | DOM cost vs cache size | Effort | Visual polish |
|---|---|---|---|---|
| Detach widget subtree, keep alive | Gemini ambient; GPT pattern 1; Claude Q1/Q5 | Would solve it, **but unsupported** | N/A | N/A |
| **Screen-per-file LRU** | Gemini P1; Claude P1 | **Active DOM constant**, cached screens off-tree | Low | Identical (no renderer change) |
| **Two-tier: focused interactive / rest flat** | Gemini P4; GPT pattern 6; Claude P3 | O(LRU × focus-chunk-tree) ≈ 30–80 | Medium | Identical for focused chunk; flat elsewhere |
| **Single flat-strip widget + islands** (Hologram / portal pool) | Gemini P3; GPT pattern 3; Claude P2 / P5 | Active DOM ≈ 1 + ≤ a few islands | High | High if islands sit cleanly over flat rendering |
| **Pure flat-strip widget, no islands** (scene viewport) | Gemini P2; GPT patterns 2 & 5; Claude P6 | Active DOM = 1 | High | **Loses** table widget interactivity |
| **Parallel structural map / overlays** | GPT pattern 4; Claude P4 | Data structure — enables others | Low–Medium | N/A |
| **Two-tier cache: scene + paint cache** | GPT pattern 4; Claude P8-ish | Memory-aware | Medium | N/A |
| **Off-main-thread encoding** | GPT pattern 7; Claude P7 | Orthogonal multiplier | Low–Medium | N/A |
| **Explicit `RenderedDocument` type** | Claude P8 | Refactor — enables everything else | Low | N/A |

### Constraint coverage per pattern

| Pattern | Match highlights / ticks (1–4) | Cross-file LRU (6, 7) | Multi-line copy (8) | Table interactivity (9) | Live query rerun (10) | 1000-page docs (11) | Cache-hit latency (A) | pilot.pause (B) | DOM scaling (C) | Prefetch (D) |
|---|---|---|---|---|---|---|---|---|---|---|
| Screen-per-file LRU | ✅ | ✅ | ✅ | ✅ | ⚠ (rebuild affected lines only) | ✅ | ✅✅ | ✅✅ | ✅✅ | ⚠ (still pays mount cost off-thread) |
| Two-tier focused/flat | ✅ | ✅ | ⚠ (cross-boundary) | ✅ in focused chunk; passive elsewhere | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Flat-strip + islands | ✅ | ✅ | ⚠ (cross-island) | ✅ in focused islands; static elsewhere | ✅ | ✅ | ✅✅ | ✅✅ | ✅✅ | ✅✅ |
| Pure flat-strip / scene viewport | ✅ | ✅ | ✅ | ❌ (no native table scroll) | ✅ | ✅ | ✅✅ | ✅✅ | ✅✅ | ✅✅ |

(✅✅ best-in-class · ✅ meets bar · ⚠ needs care · ❌ regression)

---

## The staged plan

Each stage is independent; each unlocks the next; each has a measurable
decision gate.

### Stage 0 — Verify the premise (~1 h)

**Goal.** Before any architectural commitment, confirm that suspended
Textual screens are excluded from the active screen's compositor walk.

**Action.**

- Throwaway script: two Screens, each containing ~200 `Static`
  widgets. Push screen B so A suspends.
- Instrument `Pilot.pause()` (and/or wrap `_compositor.render`) to
  count widgets actually rendered per tick.
- Confirm the count corresponds to B only, not A+B.

**Decision gate.**

- **If walked-count ≈ B only** → P1 is on the table. Proceed to Stages
  1 → 3.
- **If walked-count ≈ A+B** → P1 is dead. Skip Stage 3; proceed to
  Stages 1, 2, then Stage 4 (two-tier) or Stage 5 (strip + islands).

**Implementation notes.** No production code touched. Test lives at
`tests/perf/spike_screen_compositor.py`. Stage 0a (below) is a small
fix-up to the branch tip, independent of the spike.

### Stage 0a — Restore baseline (forward-only)

Before any new work, restore the values the empirical bench showed
were correct. Forward-only commit, preserves history:

- `_PREVIEW_CACHE_MAX_FILES = 64` (was rolled to 16 experimentally).
- `_BACKGROUND_FILL_RADIUS = 10` and `_PREFETCH_MOUNT_RADIUS = 0`
  restored (both were removed when prefetch was switched to full-
  mount).
- Prefetch loop returns to focused-chunk-only mount.
- Filter switch in `prefetch_top` (chunk_cache → preview_cache) **stays
  — that was a real bug fix.
- `_prefetch_one` chunk-cache reuse **stays** — also a real fix.
- `_finalize_via_lock` cold-mount path **stays** (commit `0850012`).
- Title-refresh fix **stays** (in `_activate_preview_container`).
- Harness, diag timestamps, leak-probe fixture **stay**.

Commit message explicitly references this plan and `preview-dom-
analysis.md`.

### Stage 1 — `RenderedDocument` refactor (~1–2 d)

**Goal.** Decouple the LRU's contents from the carrier's identity. No
user-visible behaviour change.

**Action.**

- Introduce `RenderedDocument` (or extend `FileView` in
  `fnd/tui/line_buffer.py`) carrying:
  - `strips: list[Strip]` (current `FileView.lines`-derived strips
    for flat; populated lazily for structural).
  - `structural_map: list[(line_start, line_end, kind, payload)]`.
  - `match_lines: set[int]` (drives scrollbar ticks).
- Migrate `_flat_buffer_cache` to hold `RenderedDocument` instances
  rather than widget instances.
- Build the structural map alongside `build_file_view`. Cost: one
  extra walk of the chunk list per build.
- **Reuse `_md_flat.py`** rather than write a new flattener — it
  already produces strips from markdown source; just extend it to
  emit the structural map alongside.

**Why first.** Every subsequent stage benefits; nothing else regresses.
The `_md_flat.py` reuse is the cheapest path to a structural-path
strip representation.

**Decision gate.** None — pure refactor. Existing test suite must
continue to pass.

### Stage 2 — Push structural encoding off-thread (~0.5–1 d)

**Goal.** Match the flat path's prefetch model.

**Action.**

- Move `FNDMarkdown` source preparation (in `_prefetch_one`,
  `app.py`) and any markdown-it / Rich pre-parsing into the existing
  prefetch worker thread.
- On the main thread, prefetch handoff becomes a single
  `app.mount(widget)` per chunk rather than `FNDMarkdown(...) +
  mount`.

**Why second.** Worth doing on its own; Stage 3 needs it to make
screen-prebuilding feasible.

**Decision gate.** None — pure performance optimisation. Measure on
the existing benches but don't gate.

### Stage 3 — Spike P1 (screen-per-file LRU) (~2–3 d)

**Goal.** Make active-screen DOM independent of `LRU_CAP`.

**Action.**

- Replace `PreviewCache`-of-`PreviewContainer` with `PreviewCache`-of-
  `FilePreviewScreen` (a `Screen` subclass). Use `App.add_mode` so
  result list and preview each have their own stack.
- Result-list → preview activation calls `app.switch_screen(name)`
  rather than mounting / class-toggling a sibling container.
- Cross-screen updates (live query rerun, focused-chunk band change)
  route through `app.get_screen(name).post_message(...)`.
- Drop any `query_one` calls that would walk across cached preview
  containers (they would silently fail on suspended screens).
- Keep CSS app-level (avoid screen-level CSS spillover bug
  [textualize/textual#5342](https://github.com/Textualize/textual/issues/5342)).

**Decision gate.** Measure with `tests/perf/bench_user_symptoms.py`
and `tests/perf/bench_input_lag.py`:

- `pilot.pause()` median ≤ 25 ms, p95 ≤ 50 ms under stress.
- Click-to-paint latency on a 100% LRU cache hit < 100 ms.
- DOM widget count on the active screen at steady state independent
  of `LRU_CAP`.

If gates pass at `LRU_CAP = 16+` → **ship and stop here**. Stages 4–5
remain available but unnecessary.

### Stage 3a — Layout + navigation fixes for the structural preview (~0.5 d)

Three independent fixes landed together because reproducing the
user-visible "Workshop content cut off + match navigation jumps to
unrelated area" symptoms required all three.

**A. Container content-height clip.** `PreviewContainer` is the
scrollable canvas inside `#preview_pane`. Textual's
`VerticalLayout.get_content_height` has a shortcut for "all children
are dynamic-height" that arranges them inside `container.height` —
fine for nested flex layouts, wrong here because the parent IS the
scrollable. The shortcut clamped the container's height to ~433 cells
even when its children summed to 1117, so anything past that y was
positioned in widget coords but unreachable via scroll (chunks beyond
the clip line rendered as a hard wall).

Fix: override `PreviewContainer.get_content_height` to arrange with
`Size(width, 0)`, forcing the non-shortcut branch. Each chunk widget
then reports its full intrinsic height and the pane's virtual_size
matches the content.

**B. First-paint scroll lands at the wrong y.** `_finalize_via_lock`
awaited only the focus chunk's `build_done` before revealing +
scrolling. Sibling chunks above the focus were still height=0 at
scroll time; the scroll lands at the focus's then-stale virtual_y,
then those siblings finish building, layout shifts, and the focus
ends up off-screen — the "match flashes for a frame then jumps to an
unrelated area" symptom.

Fix in `_finalize_via_lock`:

1. Await the focus chunk's `build_done`.
2. Collect every above-the-focus FNDMarkdown that's been mounted by
   phase 1b (must collect *after* the focus build resolves so the
   siblings are already in `chunk_widgets`).
3. Await each sibling's `build_done`.
4. Yield + `container.refresh(layout=True)` in a loop, up to 20
   cycles, until the focus chunk's `region.height` is non-zero — the
   compositor's coordinate pass runs on the next refresh cycle, not
   synchronously with build_done.
5. Then remove `-pre-reveal` and schedule the scroll.

**C. Scroll-driven lazy mount.** Even with the clip and finalize
fixes, the mounted set was still capped at focus ± `_VISIBLE_FIRST_*`
because `_BACKGROUND_FILL_RADIUS=3` is below the visible-window radius,
making the phase 2a/2b background-fill loops dead code at runtime.
Scrolling past the visible window hit a hard wall; chunks left in
gaps between two focus points (e.g., a low-index hit followed by a
high-index hit) never mounted at all.

`MatchAwareScroll.watch_scroll_y` now notifies the app on scroll
changes. `FNDApp._check_preview_lazy_mount` (debounced 120 ms) finds
the chunk widget at the viewport's top / bottom edge and, when the
user is within `_LAZY_MOUNT_TRIGGER_MARGIN` cells of that chunk's
boundary AND the immediately-adjacent chunk in document order is
unmounted, schedules a `_lazy_mount_batch` task to mount the next
`_LAZY_MOUNT_BATCH` chunks. Below-direction batches just append.
Above-direction batches mount hidden + reveal; viewport-anchor
preservation via virtual_region delta proved unreliable post-reveal
(returns 0 on consecutive batches even after refresh), so the
prepended chunks land at the top of the user's viewport — which IS
the right UX when they just scrolled up to the wall trying to see
what's above.

`_suppress_lazy_mount_briefly` (400 ms gate) is set before every
programmatic scroll (`_do_scroll_to_chunk`, `_do_scroll_to_widget`,
phase 2b reveal-anchor) so the navigation's own scroll-to-widget
doesn't trip the watcher and compete with the anchor.

Adjacency check (`bottom_idx + 1 not in mounted`) means the trigger
only fires when the user is genuinely about to cross from a mounted
chunk into an unmounted neighbour; long contiguous mounted spans
don't cascade-mount the rest of the file.

**Why outside the main staging.** Stage 3 (Screen-per-file LRU) is
deferred for the architecture-mismatch reason in the session-state
memory; these fixes hold the UX bar until Stage 3 is viable. All
three are localised to the structural-preview path and don't touch
the flat-buffer pipeline.

**Decision gate.** Constraint (D) — preview pre-mount must not block
the event loop. Each lazy-mount batch awaits `FNDMarkdown.build_done`
between mounts, and there is at most one lazy-mount task in flight
per active container.

### Stage 4 (conditional) — Two-tier focused/flat for structural (~1–2 w)

Take only if Stage 3 doesn't move the needle enough, or if user-
perceived mount cost on first-visit-per-file is still too high.

**Action.**

- The flat-markdown render lives in `_md_flat.py` already (Stage 1
  added the structural map).
- Per-file preview becomes a small `Vertical` with three children:
  pre-flat (`LineBufferPreview`), focused `FNDMarkdown`, post-flat
  (`LineBufferPreview`).
- On chunk-focus change: re-flatten the previously-focused chunk into
  whichever flat widget it belongs to; inflate the newly-focused
  chunk into a `Markdown` widget; adjust scroll offset to maintain
  visual continuity.
- Reuse `MatchAwareScrollBar` against a shared `match_lines` set.

**Decision gate.** Same metrics as Stage 3.

### Stage 5 (last resort) — Single carrier + table portal pool (months)

Only if Stages 1–4 still leave unacceptable tail latency at very
large `LRU_CAP` or 1000-page-document workloads.

**Action.**

- Custom `ScrollView` subclass painting from a pre-baked
  `list[Strip]` per file.
- Tiny pool of reusable absolute-positioned widgets (one
  `DataTable`, one focused-fence `Syntax`) overlaid on the focused
  chunk only.
- Stage 1's `structural_map` provides the (line_range, kind, payload)
  needed to position islands.

**Order of complexity is high; do not lead with this.**

---

## Specific code-risk callouts

These are gotchas to keep in mind during implementation:

- **Highlight-aware block subclasses** (`FNDMarkdownH1` …
  `FNDMarkdownTD` in `app.py:383+`) don't survive a move to a flat
  carrier as-is. The highlight LOGIC is portable
  (`_build_match_spans` is already pure) but the MECHANISM changes
  from "subclass `MarkdownBlock` and override `build_from_token`" to
  "post-process Strip segments". Plan rewrite time accordingly.
- **`_finalize_pre_reveal` / `_finalize_via_lock` + the multi-phase
  mount choreography** (`app.py:2211`, `app.py:2864`) exists because
  mounting `FNDMarkdown` widgets is expensive. Screens-as-LRU
  largely eliminates the REASON for this logic, but it'll need to be
  re-derived if Stage 4 reintroduces mounting elsewhere.
- **Match-scrollbar ticks** are line-precise on the flat path but
  chunk-uniform on the structural path
  (`fnd/tui/preview_scrollbar.py:20-32`). Any restructuring should
  converge both to line-precise via the structural map (Stage 1).
- **`Strip.apply_style`** has a known limitation around `post_style`
  ([textualize/textual#6448](https://github.com/Textualize/textual/issues/6448))
  that matters if a fuzzy-match highlight needs to OVERRIDE a
  syntax-coloured fence segment. Solvable but worth knowing if
  Stage 5 happens.
- **`query_one` across screens** doesn't work — non-active screens
  are not in the query tree. Anything using CSS query selectors to
  find widgets across the preview cache will silently fail. Stage 3
  requires direct references stored in the LRU dict.
- **CSS scoping bug** in `CSS_PATH` per screen
  ([textualize/textual#5342](https://github.com/Textualize/textual/issues/5342)).
  Keep CSS app-level.

---

## What NOT to do

All three AI responses agree these are dead ends:

- **Trying to patch `display: none` to skip the compositor.** Empirical
  evidence on this branch confirms it doesn't, and Textual doesn't
  expose a way to change that behaviour for a subtree.
- **Caching `Console.render` output bytes and blitting them
  directly.** Breaks selection (8), match overlays (2), and scrollbar
  ticks (4).
- **Filing a Textual issue for `Markdown` to mount fewer widgets.**
  Worth filing for posterity, but not a strategy you can ship behind
  — Frogmouth has the same problem and hasn't fixed it.
- **A full SumTree-style structural index** (à la Zed). Overkill
  given previews are immutable post-decode; a `SortedList` of
  `(line_start, line_end, kind, payload)` plus a `set[int]` of
  match-bearing lines covers 95% of the value at a small fraction
  of the complexity.
- **Subtree-level "detach but keep cache" hacks via private
  internals.** Brittle, no upstream support.

---

## Existing harnesses

The branch ships measurement infrastructure ready for Stage 0 /
Stage 3 verification:

- `tests/perf/bench_user_symptoms.py` — per-click wall-clock to
  title-update / focused-widget-mounted / first-match-block-resolved
  / widget-visible / do-scroll-completion. Captures errors from the
  diag log.
- `tests/perf/bench_input_lag.py` — `pilot.pause()` distributions by
  phase (boot, click-immediate, click-settle, between-clicks, all-
  done) and DOM widget count snapshot.
- `tests/perf/bench_prefetch_window.py` — prefetch window vs cursor
  position diagnostic; per-click cache-state table.
- `tests/perf/auto_test.py` — cold-path elapsed and scroll-count
  parsing for cold mounts.
- Diag log at `/tmp/fnd-preview-diag.log` (when
  `FND_PREVIEW_DIAG=1`), timestamped with monotonic seconds.

---

## References

- `preview-dom-analysis.md` (repo root) — primary code-anchored
  analysis. **Read first.**
- `# GPT & Gemini Responses - DOM.md` (Desktop) — the three original
  AI proposals.
- `docs/archive/RESEARCH_VS_CURRENT.md` — earlier informal comparison;
  superseded by `preview-dom-analysis.md`.
- `docs/archive/INVESTIGATION.md` — pre-compact handoff notes from
  the investigation session; useful for "what was tried and didn't
  work" context.
- Memory files: `feedback_propose_before_arch_changes.md`,
  `feedback_measure_then_implement.md` — durable lessons from this
  investigation.

---

## Branch hygiene

- **Investigation branch** (`investigation/preview-perf-2026-05-14`):
  preserved, tagged at tip as
  `investigation/preview-perf-2026-05-14-handoff`. Do not
  force-push. History is intact and recoverable.
- **Implementation branch**: starts fresh from this plan's Stage 0a
  state, named `feat/preview-dom-rework` (or whatever the next
  session chooses). Each Stage 1–5 lands as its own focused commit
  (or small commit series).

---

*Generated 2026-05-15 to consolidate `preview-dom-analysis.md`, the
three AI proposals, this branch's empirical measurements, and the
session's architectural lessons.*
