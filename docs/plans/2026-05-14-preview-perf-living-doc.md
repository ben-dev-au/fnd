# Preview performance — living investigation doc

> Note: env vars referenced below were renamed `FND_*` → `_FND_*` on 2026-05-19
> (private-knob convention). Older snapshots in this doc may show the old names.


**Status:** v0 (initial scaffold, populated from session research). **Owner:** Ben.
**Predecessor:** `~/.claude/plans/yesterday-pre-load-functionality-amongst-soft-planet.md`
(superseded only where this doc updates findings — cited as prior art, not deleted).

## How to use this doc

- Living. Update entries as evidence comes in. Don't delete refuted claims — change
  their **confidence** and add a **what changed it** line.
- Every claim/option carries a **confidence** level. The levels are:
  - **validated** — I read the relevant Textual source and ran a probe that
    confirms the claim against the version we ship (8.2.5).
  - **source-confirmed** — read the Textual source or docs, did not run a probe.
  - **report-asserted** — the Gemini or Textual layout-on-reveal report makes
    the claim. I have not verified it against source or by probe.
  - **speculation** — my reasoning, no source/probe evidence.
- "Confidence in claim accuracy" and "confidence in applicability to fnd"
  are tracked separately. A claim can be true in general (high) and still
  inapplicable to our use case (low).

---

## 1. Context & history

`fnd` is a Textual TUI search tool for local document corpora. The preview
pane has been rebuilt many times (user-reported: "17 rebuilds"). The current
shape:

- **Structural path** — `MarkdownDocument` and friends for `.md`, `.docx`,
  `.pptx`. Per-chunk widget tree: ~5–30 block widgets per chunk, ~150 chunks
  per heavy file → ~1000–4000 widgets per preview. Each cell of a markdown
  table is its own widget. (fnd/tui/app.py).
- **Flat path** — `LineBufferPreview` (`fnd/tui/line_buffer.py`) for PDF/TXT.
  Single-widget virtualized line buffer; line + span model; native
  `scroll_to_line(line_index, center=True)`.
- **Prefetch** — `_prefetch_top_results` (`app.py:2010` region) decodes top-N
  results (default `preview_prefetch_count = 10`, `config.py:247`) in a worker.
  Data caches: `_chunk_cache`, `_prebuilt_cache` (flat). Widget caches:
  `_preview_cache` (structural LRU), `_flat_buffer_cache` (flat LRU).
- **Reveal flow** — `_render_full_doc` → `_dispatch_preview_mount` /
  `_dispatch_flat_buffer_mount` → `_activate_preview_container(pre_reveal=True)`
  → `_finalize_pre_reveal` removes `-pre-reveal` (CSS `visibility: hidden`)
  after a deferred scroll-to-match.

### Recent session changes (already in working tree, uncommitted)

- Drainer pattern for prefetched widget mounts (single-consumer queue).
- `_user_mount_in_flight()` cooperative wait gating prefetch behind user mount.
- LRU `protect=` parameter to prevent evicting the active container.
- `_finalize_pre_reveal` gates reveal on the scroll-landed callback.
- `_do_scroll_to_chunk` 30-retry chain on `first_match_block` + region.height==0.
- Pre-reveal class `-pre-reveal` (`visibility: hidden`) on prefetched containers.

### Reports referenced in this doc

| ID | File | Status |
|---|---|---|
| **R-Gem** | `~/Desktop/Gemini Deep-Research2.md` | read in full this session |
| **R-Lay** | `~/Desktop/Textual layout-on-reveal performance for large Markdown widget trees.md` | read in full this session |

---

## 2. Goals

### Qualitative

- Navigating result list feels instantaneous. No spinner-bar pulse, no
  multi-second freeze on click, no jump from doc-top to match.
- Prefetch buffers around the cursor (cursor sits roughly in the middle of
  the prefetched window) so the user has to *try* to outpace it.
- Match always lands at the first match. Never the file top, never the
  chunk top.
- Architecture is durable — no more "rebuild for the 17th time".

### Concrete numeric targets — **NEEDS USER INPUT**

| Metric | Acceptable | Aspirational | Source for current value |
|---|---|---|---|
| First-visible after query (compromise warm) | ? | ? | not measured this session |
| Press-to-display for prefetched file | ? | ? | not measured this session |
| Press-to-display for out-of-prefetch file | ? | ? | not measured this session |
| Held-arrow median latency | ? | ? | not measured this session |
| Held-arrow worst latency | ? | ? | not measured this session |

**Action item:** Ben to specify acceptable / aspirational. Without these, every
proposal looks "good enough" or "not good enough" subjectively.

---

## 3. Hard requirements (standing)

These bind every option in this doc and every PR that lands from it.

1. **No AI traces in the project.** No co-author lines, no AI references in
   commits, code, docs, paths. (`memory/feedback_no_ai_traces.md`.)
2. **Succinct, load-bearing comments only.** Multi-paragraph block comments
   are an AI tell. Default to no comment; one short line when *why* is
   non-obvious. (`memory/feedback_comment_density.md`.)
3. **Fallback logging.** Any code path that falls back to a more expensive
   resolution logs at info-level when triggered (kind of failure, what was
   tried, what's being used). Applies to preview resolution, prefetch,
   decode, scroll, anywhere we have fallbacks. *Established this session.*
4. **fnd reindex.** Schema bumps auto-migrate; manual is
   `fnd collection reindex <name> --rebuild`, not `fnd index --rebuild`.
5. **No silent reveal at file-top or chunk-top.** Reveal lands at the
   literal match widget or it doesn't reveal yet. *Established this session.*

---

## 4. Current symptoms (user-reported)

| ID | Symptom | First reported | Status |
|---|---|---|---|
| **S1** | "Nothing shows in preview on query — need to navigate to a different file and back before it loads." | this session | open |
| **S2** | "Sometimes when navigating to a new file it won't show the first match either." | this session | open |
| **S3** | Red 'pre-loading bar' in shifting positions on cold loads. | yesterday's plan | open (per-yesterday-plan) |
| **S4** | Tap-navigation through results loads multiple intermediate files, each with its own lag. | yesterday's plan | open |
| **S5** | Visible jump from doc-top to match on reveal. | yesterday's plan | open |
| **S6** | Prefetch covers only top-N sequentially, doesn't follow cursor. | yesterday's plan | open (R4 in yesterday's plan was a partial fix) |
| **S7** | "1–3 second click freeze" on heavy md files. | this session | open |

---

## 5. Empirically validated findings

Each row: claim → evidence (source + probe) → confidence.

| # | Claim | Evidence | Confidence |
|---|---|---|---|
| **V1** | `display: none` filters children out before layout — 0 arrange calls on a hidden subtree. | `_arrange.py:61` `display_widgets = list(filter(_get_display, children))`. Probe: hidden group received 0 arrange calls at mount; 9 calls on reveal (only 3 cache hits). | **validated** |
| **V2** | `visibility: hidden` keeps the widget in layout — arrange/place still runs. | `_compositor.py:575-581` adds visibility:hidden widgets to `add_new_invisible_widget`; `_arrange.py` does not filter them. Probe: 35 arrange calls at mount, 0 on reveal (18 cache hits). | **validated** |
| **V3** | `position: absolute` removes from parent flow — parent y-cursor does not advance. | `layouts/vertical.py:108-122`: `if not overlay and not absolute: y = next_y + margin`. Probe: parent virtual_size.height stayed at 200 with absolute (vs 1200 with plain visibility:hidden). | **validated** |
| **V4** | Visibility flip preserves child `_arrangement_cache` (no `display=True` flag bumps `_nodes._updates`). | `css/styles.py:260` display has `layout=True, display=True`; `:273` visibility has only `layout=True`; `:329` position has neither. `widget.py:1335-1356` arrange cache key `(size, self._nodes._updates, optimal)`. | **validated** |
| **V5** | On a 100-widget synthetic tree, Absolute-Hidden reveal is ~36% faster than display:none reveal. | Probe: 429 ms vs 275 ms first-pause. | **validated** (on synthetic; real corpus unknown) |
| **V6** | gc.freeze() gave ~8 ms improvement on synthetic worst-case scroll latency. | Probe on 8 mounted markdowns. | **validated** (small N, short test) |
| **V7** | Re-parenting triggers full layout + spatial map rebuild — not a perf win. | R-Gem §"The Viability of Widget Reparenting"; R-Lay §"Staging containers and re-parenting"; matches Textual lifecycle behaviour. | **source-confirmed** |
| **V8** | No public pre-layout / off-screen layout API in Textual. | R-Lay §"Pre-layout API for a widget subtree"; Textual docs lack a `measure()` / `prelayout()` entry. | **source-confirmed** |
| **V9** | Textual's segment cache survives across visibility transitions, but spatial coordinates do not (for display:none). | R-Gem §"Internal Caching Layers During Display Transitions"; confirmed by V1 (cache invalidated on display flip). | **validated** |
| **V10** | `Markdown.update(text)` returns an `AwaitComplete` (Textual 8.2.5 `_markdown.py:1363`). Awaiting it guarantees all batched mounts (`BATCH_SIZE=200`, `_markdown.py:1385`) finish. `build_from_token` runs synchronously inside `_parse_markdown` before mount, so `first_match_block` is set by the time the awaitable returns — *for covered subclasses*. **Caveat:** `region.height` still requires layout to run, which happens on the next refresh tick after mount. So an awaited build guarantees the reference is resolved, not that the region is non-zero. | Read of Textual 8.2.5 `_markdown.py:1257-1430`. | **validated** (was U10) |
| **V11** | FND's `_HighlightingBlockMixin` covers exactly: H1–H6, Paragraph, BlockQuote, OrderedListItem, UnorderedListItem, TH, TD (`fnd/tui/app.py:325-422`). FND's `FNDMarkdown.BLOCKS` overrides exactly those keys; everything else inherits Textual's defaults. `MarkdownFence` and `code_block` (both map to `MarkdownFence` upstream — `_markdown.py:1009-1010`) are deliberately uncovered (acknowledged in `FNDMarkdown` docstring `app.py:389-392`). | Direct grep + source read. | **validated** (was U11) |
| **V12** | `MarkdownFence` content is set via constructor at `_markdown.py:1333` (`fence = fence_class(self, token, token.content.rstrip())`), bypassing `build_from_token` entirely. So matches inside fenced code blocks **cannot** populate `first_match_block` even if the mixin were added — the entry point is different. Any fence-coverage fix needs a fence-specific subclass that overrides the constructor or `on_mount` to apply highlights. | Direct source read. | **validated** |
| **V13** | 0x7c13's JIT virtualization exists, is public, and is shaped as a wholesale `Markdown` rewrite — not a patch. PR `https://github.com/0x7c13/textual/pull/2` (state OPEN against 0x7c13's fork, not merged upstream). Approach: `Markdown extends ScrollView`; parsed blocks are dataclass objects, not widgets; only visible lines render via `render_line`; binary-search line→block lookup; LRU strip cache. +1324 / -1033 lines on `_markdown.py`. Claimed perf: 2–3 s → 100–200 ms load. **Important consequence for fnd:** this is conceptually identical to what `LineBufferPreview` already does for PDF/TXT — ScrollView + Line API + virtualization. So we don't necessarily need to vendor 0x7c13's fork; we already have the infrastructure. | gh pr view of 0x7c13/textual#2. | **validated** (was U5) |
| **V14** | Python 3.14's GC changes "mostly solve" the original #6381 stutter (per `timesler` in #6381 thread). FND is on 3.13 (`python3.13` in `.venv`). | #6381 thread, gh issue view. | **validated** (relevant to U8) |

### Notable gap between R-Gem claims and my probe

R-Gem claims Absolute-Hidden reveal is "sub-10 millisecond" (line 103). My
probe on a heavy synthetic tree gave **275 ms**, not <10 ms. R-Gem's table on
line 105-109 puts the same claim ("Sub-10ms") against the two non-`display:none`
rows. Mechanism is real (cache survives), but the report's magnitude is
exaggerated — the remaining cost is compositor walk + segment generation +
paint, none of which the trick eliminates. **Don't quote sub-10ms as a target.**

---

## 6. Speculative claims pending validation

| # | Claim | Source | Why it matters | What would validate |
|---|---|---|---|---|
| **U1** | The SFO corpus first match lives in `MarkdownFence` (uncovered subclass) or another widget that doesn't set `first_match_block`. | my reasoning, S1/S2 | If true: A2-style fixes need a fallback descendant scan. If false: a covered subclass is racing. | One `logger.debug` line in `_finalize_pre_reveal` printing `type(target).__name__`. ~5 min. |
| **U2** | Table cells dominate widget-count cost on the SFO corpus. | my reasoning, R-Lay §"Reduce widget count via composition" | Drives whether DataTable replacement is worth pursuing. | Walk `_active_preview` subtree, count by widget type. ~15 min. |
| **U3** | Click-to-display worst case is dominated by mount/layout, not decode. | my reasoning | If decode dominates, Absolute-Hidden buys nothing on cold click. | Existing `--profile` plumbing on 3 representative files. ~10 min. |
| **U4** | DataTable's `cursor_coordinate` + `_scroll_cursor_into_view()` works cleanly with our content-overlay highlight model. | Perplexity answer (no source code probe) | Determines if "one widget per markdown table with cell-precision scroll" is achievable. | Tiny demo: DataTable with manually-baked Rich highlights, drive cursor to a known cell, observe scroll behaviour. ~30 min. |
| ~~U5~~ | *Moved to V13. Resolved.* Open follow-up: does fnd vendor 0x7c13's fork, or extend the existing flat path (`LineBufferPreview`) to render markdown? See new option **L7/W8** in §7. | — | — | — |
| **U6** | Absolute-Hidden's win on synthetic (~36%) generalises to SFO heavy md (which has more complex table structures). | extrapolation from V5 | Confidence in B as a phase. | After U3 baseline, build a one-off branch with `.preview-cached { position: absolute; visibility: hidden; }`, measure click-to-display delta on SFO. ~1 hr. |
| **U7** | Prefetch widget pre-mounting (paying layout cost in background) doesn't starve user keystrokes when 10 files × 1000+ widgets are mounted concurrently. | my reasoning + drainer design intent | This is the practical viability of "instant click for prefetched files". | After U6, instrument drainer; rapid-type during prefetch; capture longest event-loop block. ~30 min. |
| **U8** | gc.freeze() benefit on real corpus is more than the marginal ~8 ms seen on synthetic. | R-Gem §"Overcoming the Python GC Generational Freeze" + V6 | Whether to bother. R-Gem treats it as critical; my probe suggests marginal. | 30-minute usage session with and without `gc.freeze()` post-prefetch, count gen2 GC events + page-stutter perception. |
| **U9** | The 150 ms preview load debounce is appropriate once load is fast. | yesterday's plan F4 | If loads are instant, debounce doesn't matter. If they stay slow, debounce timing matters. | Decided by U3/U6 results. |
| ~~U10~~ | *Moved to V10. Resolved.* | — | — | — |
| ~~U11~~ | *Moved to V11 + V12. Resolved.* | — | — | — |

---

## 7. Approaches under consideration

Per area. **None of these are decided.** Trade-offs explicit, confidence
labelled.

### 7.1 Regression fix — reveal currently gated on `first_match_block` retry chain (S1, S2)

**V10/V11/V12 update:** `Markdown.update()` is an `AwaitComplete`. Awaiting it
guarantees `first_match_block` is *resolved* (None or set) for covered subclasses.
But `region.height` still requires a layout tick after mount. So "await + one
refresh + resolve" is the deterministic shape, not "await + resolve". Fences
**cannot** participate in `first_match_block` regardless — the entry point is
the constructor, not `build_from_token`. So whenever the match lives in a fence,
something has to descend.

| Option | Description | Confidence | Trade-offs |
|---|---|---|---|
| **A1: await build, then one refresh, then resolve** | Replace the 30-retry chain with: `await md.update(text)` (or equivalent post-mount awaitable) → `call_after_refresh` once → resolve `first_match_block`; if None, descendant scan. Reveal logs which path fired. | depends on whether the *fnd* mount path can be reshaped to await the per-chunk Markdown widgets (today it constructs them with `markdown=text` and mounts; the awaitable is fired implicitly via `_on_mount`). | Cleanest reveal shape. Implementation cost: needs `_mount_chunks_async` to await each chunk's `update`-equivalent, OR a separate "build complete" signal on the FNDMarkdown widget. |
| **A2: subclass `MarkdownFence` to bake highlights** | Override fence construction (constructor or `on_mount`) to apply highlight spans + register `first_match_block` when the fence content contains a match. | speculation; needs prototype | Closes the V12 gap permanently — fences participate in the same model as paragraphs. Cost: bypassing rich.syntax.Syntax styling within the highlight regions (or layering on top). Tractable scope, not architectural. |
| **B** | Keep retry chain, but cap at small N (e.g. 3); on exhaustion run `_fallback_match_target`. The existing `_fallback_match_target` already covers fences (via `getattr(w, "code", None)` at `app.py:2889`). | source-confirmed (`_fallback_match_target` already handles fence text) | Minimum change. Ships immediately. Logs make the cost visible. Doesn't address the underlying timing — just bounds it. |
| **C** | Drop pre-reveal entirely. Show the new container immediately; let the user see it scroll. | speculation | Eliminates S1. But: every load now has a visible mid-paint scroll jump (S5 reintroduced). |
| **D** | Skip `-pre-reveal` only when there's no previous preview (cold start). | speculation | Partial fix. Helps cold start (no blank pane) but doesn't address the underlying gating bug. Additive to A or B. |

### 7.2 Layout-on-reveal cost (S7, partly S4)

| Option | Description | Confidence in claim | Confidence in applicability | Trade-offs |
|---|---|---|---|---|
| **L1: status quo (`display: none`)** | What we have. | validated | high | Reveal pays full layout. ~400 ms+ on heavy md. |
| **L2: Absolute-Hidden Stacking (R-Gem)** | `position: absolute; visibility: hidden;` on prefetched containers. Flip to `position: relative; visibility: visible;` on reveal. | **V1–V4 validated mechanism**; **V5 validated speedup on synthetic (~36%, not sub-10ms)**; **U6 untested on real corpus** | medium | Mount cost moves to prefetch time. All prefetched chunks live in layout memory. Future Textual upgrade could regress visibility's no-`display=True` invariant. |
| **L3: ContentSwitcher (R-Lay)** | Mount all prefetched previews into a ContentSwitcher; only active child participates in layout. | source-confirmed (Textual docs) | medium | Switcher's region is bounded, so non-active children don't grow scroll. Switch is a layout pass for the new child — does it preserve their internal layout cache? Unknown. May be no better than display:none flip. |
| **L4: Staging container off-pane (R-Lay)** | Mount prefetched previews in a docked/layered staging area outside the scrollable pane; on selection, move into the visible pane. | source-confirmed | low | Re-parent = remove+mount = full layout cost on reveal (per V7). Loses the win. |
| **L5: Secondary screens (R-Lay)** | Each prefetched preview lives on its own inactive Screen. | source-confirmed | low | Each screen has its own layout. Switching screens still re-runs layout on activation. Heavier than L2. |
| **L6: JIT block virtualization (R-Gem §3)** | Subclass `MarkdownDocument`; intercept token stream; mount block widgets only when in viewport; unmount on exit. Cited: 0x7c13 #6381 work claims 2-3s → 100-200 ms. | report-asserted, **U5 untested** | medium-high if feasible | Largest architectural change. 1–2 weeks if 0x7c13's code ports cleanly; longer if from scratch. Preserves all Markdown features. Once built, every other layout option becomes redundant. |

### 7.3 Widget-count reduction (S7)

| Option | Description | Confidence | Trade-offs |
|---|---|---|---|
| **W1: status quo (per-cell widgets)** | What we have. | validated | High count. Cells are the worst offender (per **U2** untested). |
| **W2: Markdown table → Rich.Table inside one widget** | Render whole table with `rich.Table` inside a single widget. ~20 widgets per 4×5 table → 1. | source-confirmed | Cell-precision scroll: lost (only manual `region.y + row_offset` math). Inline markdown: lost within cells (we bake Rich markup). Highlight: bake spans at build time. **User position: not acceptable without scroll precision.** |
| **W3: Markdown table → DataTable** | Replace MarkdownTable tree with a `DataTable` widget. `cursor_coordinate` + `_scroll_cursor_into_view()` for cell precision. | report-asserted (Perplexity); **U4 untested** | 1 widget per table. Cell-precision scroll: native. Inline markdown in cells: lost (cells are Rich Renderables). Highlight: bake as Rich spans at build time. Loses per-cell focus + link_clicked. |
| **W4: Group inline blocks (R-Lay §3)** | Inline spans (bold/italic/code) already grouped into block widgets by Textual's Markdown. | source-confirmed | Already done. No change available here. |
| **W5: Pool widget instances (R-Lay §3)** | Reuse widget instances across previews. | report-asserted | Significant lifecycle complexity. R-Gem reports widget pooling shifts garbage but not layout cost. Probably not the main win. |
| **W6: Render chunk as Rich (P1)** | One widget per chunk, internal Rich rendering. | speculation; previously rejected | Smallest widget count. Loses per-block scroll precision. **Previously implemented and removed** — likely not a path. |
| **W7: JIT mount per block (P2)** | Same as L6. Listed here too because it addresses widget count without losing features. | ~~U5~~ V13 — exists as 0x7c13/textual#2; not merged upstream; ~1400 LoC rewrite | See L6. Vendoring the fork forks fnd off upstream Textual permanently for the Markdown widget. Likely never merges upstream (changes Markdown's public shape — ScrollView base, dataclass blocks). |
| **W8 / L7: render markdown via the flat path (extend `LineBufferPreview`)** | Recognition that fnd's flat path **already is** the ScrollView + Line API + virtualization model 0x7c13's fork applies to upstream Markdown. Render md to styled lines (via `rich.markdown.Markdown` → Console capture, or our own walk over markdown-it tokens to Rich segments) and feed into the existing flat buffer. Bake highlights as Rich spans at build time — flat path already does this for PDF/TXT. Single widget per file. | speculation; needs prototype | **Pros:** stays in fnd's existing architecture; single code path for all preview types; eliminates GC stutter + per-block layout cost at the root; we already own the scroll-to-line machinery. **Cons:** loses upstream's `MarkdownFence` scrollable code blocks, link_clicked event, per-block focus, MarkdownTable interactivity. Equivalent to W6 (chunk-Rich) **at the file level**, but cleaner because we don't need a wrapping widget per chunk — just append to the flat buffer. Worth comparing in a Tier-3 prototype against vendoring 0x7c13's fork. |

### 7.4 Scroll precision (interacts with 7.3)

| Option | Granularity | Confidence | Notes |
|---|---|---|---|
| **P-Cell-Widget** | Per-cell widget (current) | validated | Native. Lands at the literal cell widget. |
| **P-DataTable** | Cell via `cursor_coordinate` | report-asserted, **U4 untested** | Works if W3 is adopted. |
| **P-Line-Span** | Line + char span (flat buffer) | validated | Works in `LineBufferPreview` today. Available to structural path only if we extend flat rendering to md (loses inline markdown). |
| **P-RegionMath** | `table_widget.region.y + per_row_offset` | speculation | Possible with W2 but custom code, no Textual API. Brittle across re-wrap. |

### 7.5 GC behaviour

| Option | Description | Confidence | Trade-offs |
|---|---|---|---|
| **G1: nothing** | Current. | validated | gen2 pauses possible on long sessions per R-Gem / #6381. Magnitude in fnd unmeasured (**U8**). |
| **G2: gc.freeze() post-prefetch** | One `gc.collect(); gc.freeze()` after initial prefetch settles. | report-asserted; V6 marginal | Frozen objects never collected. Memory leak if prefetch churns indefinitely. FND's LRU bounds prefetch pool, so finite. Behind config flag. |
| **G3: weakref refactor (maintainer fix per #6381)** | Convert `Styles.node` from strong ref to weakref. Upstream change. | report-asserted | Not actionable in fnd until/unless we vendor a patched Textual or wait for upstream. |

### 7.6 Cursor-following prefetch (S6 — original plan intent)

| Option | Description | Confidence | Trade-offs |
|---|---|---|---|
| **F1: current (gated)** | `_fire_pending_preview_load` only re-anchors prefetch when `parent_id not in _chunk_cache` (`app.py:1539-1540`). | validated | Bug: once a file is decoded (cache hit), prefetch never re-anchors around the cursor. Cursor outpaces prefetch on long lists. |
| **F2: drop the gate** | Always call `_prefetch_top_results(anchor_parent_id=parent_id)` on settle. Worker is exclusive-group; previous worker cancels. | speculation | Should be cheap. Need to confirm cancellation doesn't drop in-flight widget mounts. |
| **F3: window-based (cursor centered)** | Compute target prefetch set as `[cursor - N/2, cursor + N/2]`; cancel files outside. | speculation | Matches the original "cursor sits in the middle of the buffer" intent. More logic than F2. |
| **F4: parallel decode of N targets** | Yesterday's plan R4 — `asyncio.Semaphore` to decode N files concurrently. | report-asserted (yesterday's plan) | More complex. Win depends on decode being the bottleneck (likely true for cold PDFs, not for fast-decode txt). |

### 7.7 Progress UI (S3 — partly addressed by yesterday's plan R1, R5–R7)

Not the focus of this doc, but tracked here for completeness. Yesterday's
plan proposed a centralised `ProgressFacility` (`fnd/tui/progress.py`,
already exists in the working tree per `?? fnd/tui/progress.py`). Not
re-evaluated this session.

---

## 8. Investigation plan

Sequenced by cost. Each entry: estimated effort, answers, depends-on.

### 8.1 Tier 1 — under 30 minutes total, answers Tier-1 unknowns

| # | Action | Answers | Status |
|---|---|---|---|
| **I1** | `_diag_log` helper + log points in `_do_scroll_to_chunk`, `_fallback_match_target` outcomes, and `_finalize_pre_reveal` start/end timing. Env-gated by `FND_PREVIEW_DIAG=1`. | **U1** | **drafted, awaiting user run** (see §8.5) |
| **I2** | `action_diag_dump_preview` bound to `Ctrl+Shift+D`. Dumps per-chunk + total widget counts to `/tmp/fnd-preview-diag.log`. Always on (no env gate). | **U2** | **drafted, awaiting user run** (see §8.5) |
| **I3** | Read `Markdown.update()` source in installed Textual; confirm awaitable + lifecycle. | **U10 → V10** | **done** |
| **I4** | Grep `_HighlightingBlockMixin` subclasses in `fnd/tui/app.py`; cross-check what's NOT subclassed. | **U11 → V11, V12** | **done** |

### 8.2 Tier 2 — under 1 hour each, measure baseline + validate L2

| # | Action | Answers | Effort |
|---|---|---|---|
| **I5** | Run `--profile` on cold start + click sequences across (a) small md, (b) SFO heavy md, (c) big PDF. Capture: first-visible, press-to-display warm, press-to-display cold, held-arrow median + worst. | **U3** + baseline | 30 min |
| **I6** | Build a throwaway branch flipping prefetched containers from `display: none` to `position: absolute; visibility: hidden;`. Re-run I5's click-to-display on SFO. Compare. | **U6** | 1 hr |
| **I7** | Instrument drainer in I6 branch; rapid-type during prefetch; capture longest event-loop block. | **U7** | 30 min |

### 8.3 Tier 3 — half-day to multi-day, validate big-architecture bets

| # | Action | Answers | Effort |
|---|---|---|---|
| **I8** | Build a DataTable demo with manually-baked Rich highlight spans + `cursor_coordinate` cell scroll. Confirm Textual's overlay highlights don't conflict. | **U4** | 30 min |
| **I9** | Find 0x7c13's code (cited in R-Gem ref 29; not linked in the report — likely in Textual #6381 comments). Read; estimate port effort to fnd. | **U5** | 2–4 hr |
| **I10** | gc.freeze() session test: 30 min real usage with and without. Counter on gen2 GC events; subjective perception of stutters. | **U8** | 1 hr |

### 8.4 Tier 4 — only after Tier 1–3 returns data

To be decided based on what we find. Possible items:
- Prototype W3 (DataTable for MarkdownTable) on one chunk.
- Prototype L2 (Absolute-Hidden) on the full prefetch path.
- Prototype L6/W7 (JIT virtualization) if I9 looks tractable.

### 8.5 How to run the I1 + I2 diagnostics

Both patches live in the working tree (uncommitted) on `fnd/tui/app.py`.
22 preview tests pass, import is clean.

**To capture the SFO match path (I1):**

```
rm -f /tmp/fnd-preview-diag.log
FND_PREVIEW_DIAG=1 fnd  # or whatever your usual launch is
# type the SFO "compromise" query, let the preview settle
# (if symptom S1 fires, navigate away and back so the preview loads)
cat /tmp/fnd-preview-diag.log
```

Expected log lines per reveal:
- `finalize_pre_reveal start seq=<N> parent_id=<id>`
- One or more `do_scroll seq=<N> ...` lines — the most informative tells you:
  - `path=match_targets` or `header` — initial resolution route
  - `path=first_match_block(<TypeName>)` — covered subclass claimed the match
  - `path=fallback(<TypeName>)` — descendant scan fired; `<TypeName>` is the
    widget the match was found in (e.g. `MarkdownFence`)
  - `result=chunk-top` — fallback exhausted, scrolled to chunk top (the
    S2 symptom)
  - `first_match=True/False`, `fallback=True/False`, `retries_used=<n>`
- `finalize_pre_reveal done seq=<N> elapsed_ms=<X>`

**To capture per-chunk widget counts (I2):**

While a preview is on screen, press `Ctrl+Shift+D`. A notification confirms
the dump. Repeat for:
1. SFO heavy md with the "compromise" query open.
2. A small md file (your pick).
3. A PDF (the flat path) for comparison.

`/tmp/fnd-preview-diag.log` will contain `--- dump_preview ---` blocks
with per-chunk counts and grand totals.

**Reverting:** the diagnostic is contained to:
- `_diag_log` method (added)
- `action_diag_dump_preview` method (added)
- `Binding("ctrl+shift+d", ...)` line in `BINDINGS`
- ~25 lines of log calls inside `_do_scroll_to_chunk` and `_finalize_pre_reveal`

A `git diff fnd/tui/app.py` shows the full surface. Easy to revert when
findings are in.

---

## 9. Decision framework

Once Tier 1 + Tier 2 data is in, the decision tree should be roughly:

1. **If I1 says the SFO match is in a covered subclass** → there's a bug in
   the mixin or in `_finalize_pre_reveal`'s timing. Fix that first (option B
   in §7.1: bounded retry + descendant fallback + logs). The big architecture
   questions can wait.
2. **If I1 says the SFO match is in `MarkdownFence`** → confirms V12's
   prediction. Option B handles this immediately (the existing fallback
   already scans for fence content via `getattr(w, "code", None)`). Option A2
   (fence subclass that bakes highlights) is a longer-term cleaner fix.
3. **If I5 shows mount dominates click latency on heavy md** → L2
   (Absolute-Hidden) is worth pursuing as a near-term mitigation. I6
   confirms or refutes. If it confirms, ship L2 behind a flag.
4. **If I5 shows decode dominates** → L2 buys nothing on cold click. Look
   at F2/F3/F4 (cursor-following prefetch) instead.
5. **If I2 shows tables dominate widget count and I4 confirms cells are the
   per-chunk worst** → W3 (DataTable) experiment is worth I8.
6. **V13 changed the architecture-bet shape.** 0x7c13's fork exists but
   is a ~1400-line wholesale rewrite of Textual's Markdown. The decision
   for the durable path is now between:
   - **W8/L7** — extend fnd's existing flat path to handle md. Stays
     within fnd. Loses upstream Markdown's interactive features.
   - **Vendor 0x7c13's fork** — keeps Markdown widget interactivity.
     Permanently forks from upstream Textual on Markdown rendering. Higher
     maintenance burden (track upstream changes manually).
   - **Stay with current arch + L2 + W3** — keeps everything; ships
     mitigations not cures.

   Pick after the regression fix lands. The trade-off pivots on how much
   the user values upstream Markdown's interactive features (link_clicked,
   scrollable code fences, per-block focus) vs. architectural simplicity.

---

## 10. Open questions (for Ben)

1. **Numeric targets in §2.** Without these, we can't say "this is fast enough".
2. **Scope:** does this doc cover the progress-UI work from yesterday's plan
   (R1, R5–R7), or is that closed?
3. **Budget:** Tier 1 = 30 min; Tier 2 = ~2 hr; Tier 3 = ~half day; Tier 4 =
   open-ended. How far down the tree do I keep going before checking back?
4. **L6/W7 (JIT virtualization):** is this on the table as a multi-week
   investment if the smaller mitigations don't get us there?
5. **FND's preview file size distribution:** the reports assume 4000-widget
   trees. Is SFO's heavy md actually in that range, or are we below/above?
   (I2 will tell us.)
6. **Are there other reports / sources you want incorporated?** Perplexity's
   table-scroll answer is captured here; if there are others, point me at
   them.

---

## 11. Out of scope

- Refactoring worker dispatch into pure-coroutine model. Yesterday's plan
  ruled this out; nothing this session changes that.
- Tuning `preview_load_debounce_ms`. Per yesterday's plan F4, decision
  follows from making load fast (U3/U6); standalone tuning has been tried.
- Indexing / cache-rebuild use cases for the progress facility.
- Inline-block widget consolidation (R-Lay W4) — already done by Textual.
- Re-implementing functionality that was already explicitly removed
  (architectural Rich chunk-rendering, W6). Targeted Rich (W3) is in scope.

---

## 12. Living-doc changelog

| Date | Author | Change |
|---|---|---|
| 2026-05-14 | Ben + session | v0 — initial scaffold from session research + both reports read in full. |
| 2026-05-14 | session (Tier-1 work) | v0.1 — I3 + I4 resolved (U10 → V10, U11 → V11+V12). §7.1 updated to reflect V12 (fences can't participate in `first_match_block` via the mixin path; need fence subclass or descendant scan). I1 + I2 diagnostic patches landed in working tree on `fnd/tui/app.py` (22 preview tests still pass). §8.5 documents how to run them. |
| 2026-05-14 | session (I9 partial) | v0.2 — I9 partial: located 0x7c13's JIT virtualization (V13 — `0x7c13/textual#2`, OPEN, ~1400 LoC rewrite). FND's flat path already implements the same conceptual model. Added option **W8/L7** (extend flat path to markdown) to §7. V14 captures the Python 3.14 GC observation. §9 decision framework restructured to reflect that the architecture-bet question is now "flat-path-for-md vs vendor 0x7c13 vs stay-and-mitigate", not "is JIT feasible". |

<!-- Add entries here as findings come in. Don't delete refuted claims; update
their **confidence** and add a 'what changed it' note. -->
