# Preview-pane DOM architecture — proposal comparison and recommended path

A read of the three AI proposals (Gemini, GPT-5.4, Claude 4.7) against `fnd`'s
current preview-pipeline code, plus a staged next-step plan.

---

## TL;DR

1. **You're not as far from the answer as the proposals assume.** `LineBufferPreview`
   in `fnd/tui/line_buffer.py` is already a fully working version of the "flat
   strip-buffer" pattern that GPT calls "scene-based viewport" and Claude 4.7
   calls P2/P6. It just happens to be wired only to PDF/TXT today. The
   architectural bet is already half-paid.
2. **The DOM problem only exists on the structural path** (`md`/`docx`/`pptx` →
   `FNDMarkdown` chunk widgets inside `PreviewContainer`). All three proposals
   are essentially answering one question: *how do we get the structural path to
   the same DOM-cost profile as the flat path, without losing visual polish?*
3. **All three responses converge on the same diagnosis** (display:none does not
   skip the compositor; cache widget output, not widget instances; one
   carrier widget + decoration overlays beats per-block widget trees). They
   disagree on the order of operations, not the destination.
4. **The single highest-leverage spike is P1 (screen-per-file LRU)**, on the
   strength of Claude 4.7's specific Textual finding that suspended screens are
   alive in Python but excluded from the active screen's compositor walk. If
   that claim holds when measured, it unlocks the cache problem with no
   rendering changes at all.
5. **Recommended path is staged**, not "pick one pattern": land a
   `RenderedDocument` refactor and off-thread encoding for the structural path
   first, then spike P1, then fall back to two-tier (P3) only if P1's measured
   savings aren't enough. Full scene-renderer with island widgets is a last
   resort, not a first move.

---

## Current architecture (anchored in the code)

The preview pane runs **two parallel pipelines** chosen per-file by
`choose_preview_mode()` (`fnd/tui/preview_dispatcher.py:34`).

**Flat pipeline — PDF / TXT.** One `LineBufferPreview` widget per file
(`fnd/tui/line_buffer.py:195`). It's a `ScrollView` subclass whose
`render_line(y)` paints from a pre-built `list[Strip]`. Match highlights are
baked into Rich spans at `build_file_view` time (`line_buffer.py:121`);
chunk-boundary gap rows are part of the line array; a parallel `match_lines`
set drives `MatchAwareScrollBar` for line-precise scrollbar ticks. There's a
separate LRU (`_flat_buffer_cache`, max 8), and the prefetch worker pre-renders
the strips off-thread (`app.py:2174`). **The "scene-based viewport" architecture
is already shipped for these formats.**

**Structural pipeline — MD / DOCX / PPTX.** Each chunk becomes an
`FNDMarkdown` widget (`app.py:383`), which is `textual.widgets.Markdown`
subclassed with `_HighlightingBlockMixin` for inline highlight overlays.
Headings, paragraphs, lists, blockquotes, and table cells get highlight-aware
subclasses; code fences deliberately keep the stock Rich-syntax renderer
(`line_buffer.py` comment / `app.py:262`). All of a file's chunk widgets live
inside a `PreviewContainer` (`app.py:153`) — a Textual `Container` that the
code calls "per-file preview container holding the mounted chunk widgets". The
LRU is a `PreviewCache` of `PreviewContainer` instances (`app.py:191`), and
non-active containers are kept in the DOM via class toggle
(`PreviewContainer.-hidden { display: none; }`).

**Mount lifecycle (structural).** `_mount_chunks_async` (`app.py:2453`) runs a
four-phase fill: (1a) focus chunk, (1b) visible window `_VISIBLE_FIRST_ABOVE/
BELOW = 7` each side, (2a) below-window fill capped at
`_BACKGROUND_FILL_RADIUS = 200`, (2b) above-window fill with `display = False`
on each mounted widget so the focused chunk's screen position doesn't drift,
then a bulk reveal.

**Where the DOM blows up.** Each `FNDMarkdown` block expands to several
`MarkdownBlock` descendants (heading rows, paragraph spans, fence containers,
table cells, etc.). For a markdown file with N chunks, the steady-state DOM
contribution is in the order of `N × (5–30)` widgets. With `LRU_CAP = 8` cached
files, that's why the doc cites ~3000 widgets choking the compositor. Every
one of those is a child of the active `Screen`'s tree, including the
`display: none` ones — which is exactly what the proposals identify as the
core failure mode.

---

## What all three responses agree on

A handful of points are unanimous across Gemini, GPT, and Claude 4.7:

- **`display: none` does not exempt a widget from the compositor walk.** Your
  own measurement is consistent with this.
- **The unit of caching should not be a mounted widget tree.** Cache either
  (a) the rendered output (`Strip`s / segments), (b) a normalized document
  scene + structural map, or (c) a `Screen` containing the tree but outside the
  active compositor's reach.
- **One carrier widget + overlay/decoration layers beats per-block widget
  trees** for the bulk of any document.
- **Off-main-thread encoding is necessary** regardless of carrier choice.
- **A two-tier "focused chunk interactive, the rest flat" arrangement** is the
  pattern most likely to survive contact with reality — it preserves table /
  fence interactivity where the user actually cares (the focused chunk) while
  capping DOM cost everywhere else.

---

## Patterns at a glance

Each row is the same pattern under whatever name the responses used:

| Pattern (canonical name)              | Gemini | GPT-5.4   | Claude 4.7 | DOM cost vs cache size | Effort | Visual polish |
| ------------------------------------- | ------ | --------- | ---------- | ---------------------- | ------ | ------------- |
| Detach widget subtree, keep alive     | —      | Pattern 1 | —          | Would solve it, **but unsupported** | N/A | N/A |
| **Screen-per-file as LRU**            | P1     | (implicit in pattern 2 lineage) | **P1** | **Active DOM constant, cached screens off-tree** | Low | Identical (no renderer change) |
| **Two-tier: focused interactive / rest flat** | P4     | Pattern 6 | **P3**     | O(LRU × focus-chunk-tree) ≈ 30–80 | Medium | Identical for focused chunk; flat elsewhere |
| **Single flat-strip widget + islands** (Hologram / portal pool) | P3     | Pattern 3 | **P2 / P5** | Active DOM ≈ 1 + ≤ a few islands | High | High if islands sit cleanly over flat rendering |
| **Pure flat-strip widget, no islands** (scene viewport) | P2     | Pattern 2 / 5 | P6     | Active DOM = 1               | High | Loses table/widget interactivity; equals current flat path's polish |
| **Parallel structural map / overlays** | —      | Pattern 4 | **P4**     | (Data structure — enables others) | Low–Medium | N/A |
| **Two-tier cache: scene + paint cache** | —      | Pattern 4 | (rolled into P8) | Memory-aware | Medium | N/A |
| **Off-main-thread encoding**          | —      | Pattern 7 | **P7**     | (Orthogonal multiplier) | Low–Medium | N/A |
| **Explicit `RenderedDocument` type**  | —      | (implicit) | **P8**    | (Refactor — enables everything else) | Low | N/A |

Constraint coverage (1–11 = user's hard constraints; A–D = perf targets):

| Pattern | Match highlights / ticks (1–4) | Cross-file LRU (6,7) | Multi-line copy (8) | Table interactivity (9) | Live query rerun (10) | 1000-page docs (11) | Cache-hit latency (A) | pilot.pause (B) | DOM scaling (C) | Prefetch (D) |
| ------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Screen-per-file LRU                  | ✅ | ✅ | ✅ | ✅ | ⚠ (rebuild affected lines only) | ✅ | ✅✅ | ✅✅ | ✅✅ | ⚠ (still pays mount cost off-thread) |
| Two-tier focused/flat                | ✅ | ✅ | ⚠ (cross-boundary) | ✅ in focused chunk; passive elsewhere | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Flat-strip + islands                 | ✅ | ✅ | ⚠ (cross-island) | ✅ in focused islands; static elsewhere | ✅ | ✅ | ✅✅ | ✅✅ | ✅✅ | ✅✅ |
| Pure flat-strip / scene viewport     | ✅ | ✅ | ✅ | ❌ (no native table scroll) | ✅ | ✅ | ✅✅ | ✅✅ | ✅✅ | ✅✅ |

(✅✅ = best-in-class, ✅ = meets bar, ⚠ = needs care, ❌ = regression)

---

## Pattern-by-pattern analysis

### 1. Screen-per-file LRU (Gemini P1 / Claude 4.7 P1)

**Idea.** Replace the per-file `PreviewContainer` (currently a child of the
active screen) with a per-file `Screen` installed via `App.install_screen()`
and switched via `App.switch_screen()`. Textual's compositor walks the *active
screen's* tree only; suspended screens stay alive in Python (with their widget
state and `_compositor` per-widget caches intact) but contribute zero per-tick
cost.

**Pros.**

- **Zero rendering changes.** Everything inside the cached screen is your
  current `FNDMarkdown` tree. Tables, fences, links, heading CSS, highlight
  overlays — all preserved exactly.
- **Cheapest cache-hit possible.** `switch_screen` fires a `ScreenSuspend` /
  `ScreenResume` pair; nothing mounts or unmounts. Strip caches survive.
- **Active DOM is independent of LRU size.** This is the actual problem you set
  out to solve.

**Cons / risks.**

- Hinges on Claude 4.7's claim about `_screen_stacks` semantics being correct.
  *That's a 15-minute measurement you can do before committing.* Build two
  screens, push the second so the first suspends, count widgets the compositor
  walks per tick on the second.
- Screen-level CSS scoping has a known spillover bug (`textualize/textual#5342`).
  Keep CSS app-level rather than per-screen.
- `query_one` doesn't see widgets in non-active screens. You'd need to drop any
  cross-screen `query_one` lookups in favour of direct references stored in the
  LRU dict (which is mostly what `PreviewContainer.chunk_widgets` already does).
- Cross-screen messaging (e.g. result-list cursor change → preview screen
  update) requires `app.get_screen(name).post_message(...)` rather than direct
  attribute mutation. Worth centralising in a coordinator.
- Live query reruns (constraint 10) still need to walk the cached screen's
  widgets to update spans, but only for the *active* screen's chunks — same
  cost as today.

**Evidence quality.** Strong. Will McGugan stated the behaviour directly;
Textual's mode system and Posting/Harlequin use the same primitive.

### 2. Two-tier: focused-chunk interactive, rest flat (Claude 4.7 P3 / GPT pattern 6 / Gemini P4)

**Idea.** Per-file preview = three regions stacked vertically: flat
`LineBufferPreview` for chunks before focus, one `FNDMarkdown` widget for the
focused chunk, flat `LineBufferPreview` for chunks after focus. On focus
change, re-flatten the previously-focused chunk into the appropriate flat
widget and inflate the newly-focused chunk into a `Markdown` widget.

**Pros.**

- **Reuses what already works.** `LineBufferPreview` and `build_file_view`
  already exist and already handle highlights, chunk gaps, scrollbar ticks,
  and multi-line selection for plain text. Extending `build_file_view` to
  accept structured `body_md` chunks is incremental, not green-field.
- **DOM cost is bounded and predictable.** ≈ `3 + (focused chunk's
  FNDMarkdown subtree)` ≈ 30–60 widgets per cached file. With LRU = 8 that's
  ~240–480 — well under your unresponsiveness threshold.
- **Polish in the focused chunk is identical to today.** Tables scroll,
  fences syntax-highlight, links style as today.

**Cons / risks.**

- **Visual continuity at the boundary.** The flat and structural representations
  of the same chunk must produce identical line counts (and ideally identical
  vertical metrics) or you'll see drift when the focus boundary moves. Your
  `_md_flat` equivalent doesn't exist yet for `body_md` — you'd need a
  flattener that produces line-accurate `Strip`s from markdown source. Doable
  but not free.
- **Selection across boundary** is the same edge case Claude 4.7 calls out for
  P2. The pragmatic answer is to keep the outer `VerticalScroll` as the
  selection coordinator and accept that selecting *into* a non-flat focused
  chunk is one selection operation; selecting prose is another.
- **Scrollbar ticks span three widgets.** You'd want a custom scrollbar
  reading a shared `set[int]` rather than per-widget `match_lines`. Mostly an
  extraction of code that already exists in `MatchAwareScrollBar`.

**Evidence quality.** Strong. Pattern is used in CodeMirror 6, Emacs folds, and
notebook editors. Two-tier is what GPT singles out as the most plausible
focused-chunk strategy *if portalized*.

### 3. Single flat-strip widget + interactive islands (Gemini P3 / GPT pattern 3 / Claude 4.7 P2/P5)

**Idea.** Each file is one custom `ScrollView` painting from a pre-baked
`list[Strip]`. For currently-focused complex blocks (tables especially), mount
a tiny pool of reusable widgets (`DataTable`, etc.) on a `layer: top` /
absolute-positioned layer over the flat rendering's footprint.

**Pros.**

- **Active DOM is essentially constant** regardless of file size or LRU size.
- Preserves table interactivity *where it matters* (focused/visible).
- Cleanest theoretical answer; what Zed, VS Code's view zones, and ProseMirror
  node-views all do.

**Cons / risks.**

- **Highest implementation cost.** You're writing a custom Markdown→Strip
  compiler that has to reproduce heading CSS, list indentation, blockquote
  bars, link styling, fence syntax-highlighting, and table layout in pre-baked
  segments. Even though `rich.markdown.Markdown` will produce these
  segments for you, the coordinate-mapping for islands is fiddly.
- **Coordinate drift** on terminal resize / wrap changes — every resize must
  reflow the strip array and recompute island bounds atomically.
- **Cross-widget selection** is a known hard problem (same as P2).
- `Strip.apply_style` has the `post_style` limitation (textual #6448) for
  override-on-top behaviour, which matters if a fuzzy match lands inside a
  syntax-coloured fence.

**Evidence quality.** Strong pattern; weak operational evidence in Textual
specifically. `DataTable` and `RichLog` prove the carrier scales; the
island-overlay piece is less travelled in this exact framework.

### 4. Pure flat-strip viewport, no islands (Gemini P2 / GPT patterns 2&5 / Claude 4.7 P6)

**Idea.** Same as #3 but skip the islands — table scrolling becomes a keybind
("when cursor is on a table, ← → scroll the table horizontally inside the
strip buffer"), fences are pre-highlighted in segments. Effectively: extend
your current `LineBufferPreview` to handle markdown's structural features.

**Pros / cons.** Same as #3 minus the polish-via-widgets benefits. This is
where `glow` and `mdcat` live. Probably *not* what you want if you've already
invested in the structural preview because it's a fidelity regression for
tables.

### 5. Parallel structural map / RenderedDocument refactor (GPT pattern 4 / Claude 4.7 P4 & P8)

**Idea.** Independent of carrier choice: introduce a `RenderedDocument` value
type that holds (a) the flat `list[Strip]` (when applicable), (b) a structural
map of `(line_start, line_end, kind, payload)` intervals, and (c) live
`match_lines`. The LRU caches *this*, not widgets. Whichever carrier is in use
reads from it.

**Pros.**

- **Pure refactor — no behaviour change up front.** Cost is one extra walk of
  the AST at decode time.
- **Decouples cache lifetime from carrier choice.** You can A/B-test patterns
  on the same cached objects.
- Live-query updates become "swap `match_lines`, refresh carrier" — same in
  every pattern.
- Your existing `FileView` is most of the way there already
  (`line_buffer.py:64`). Generalising `FileView` to also store
  `(chunk_id, kind, body_md)` per chunk is incremental.

**Cons.** None worth listing if you're already going to pick *any* of the
other patterns — this just sits underneath them.

### 6. Off-main-thread encoding (GPT pattern 7 / Claude 4.7 P7)

Already in place for the **flat** path: the prefetch worker runs
`build_file_view` + `_render_lines` in a thread (`app.py:2172`). Not in place
for the structural path — `_mount_chunks_async` does the `FNDMarkdown(...)`
construction on the main thread, which is where most of the markdown-it /
Rich parsing cost lives.

**Pro.** Cheapest measurable latency win available regardless of pattern.
**Con.** GIL-bound; markdown-it and Rich are mostly Python. Threading helps but
multiprocessing would help more if you ever needed to scale further.

### 7. Detached subtree with retained render strips (Gemini ambient / GPT pattern 1 / not-recommended)

Explicitly ruled out by GPT and Claude 4.7. No supported Textual API; relying
on private internals would be brittle. **Skip.** Your `display: none`
measurement is the empirical confirmation.

---

## Where the three responses meaningfully disagree

1. **Should the first move be "fix the cache" (P1) or "fix the renderer" (P2/
   P3)?**

   - Gemini leads with P3 (Hologram) as the "ultimate fix" — fix the renderer.
   - GPT explicitly rules out the screen-stack mechanism (its pattern 1) as
     unsupportable and points hard at scene-based viewport — fix the renderer.
   - Claude 4.7 picks P1 because of the Will McGugan finding — fix the cache.

   **Resolution.** Claude 4.7 cites direct evidence (the discussion-board quote
   and the `_screen_stacks` internals); GPT explicitly didn't have that
   evidence and was conservative. The screen-stack approach is verifiable in
   under an hour. *Verify before committing*: if the compositor really does
   walk only the active screen, P1 is the cheapest path. If it doesn't, fall
   back to GPT/Gemini's read.

2. **How much polish do you actually lose moving to a flat strip carrier?**

   - Gemini implies near-zero loss with Hologram.
   - GPT distinguishes "visual appearance" (no loss) from "Textual-CSS-as-
     widget-classes" (real loss).
   - Claude 4.7 agrees with GPT.

   **Resolution.** GPT/Claude 4.7's framing is more accurate. Your highlight-
   aware block subclassing in `app.py` is a clean per-widget extension pattern
   today; in a flat carrier, the same highlight logic would live inside a
   Strip-transformation function. Visually identical, conceptually different.

3. **Where does table interactivity live?**

   - Gemini: absolute-positioned `DataTable` over the flat render (Hologram).
   - GPT: portal pool — *one* reusable `DataTable` for whichever table is
     currently focused.
   - Claude 4.7: "islands" — same as GPT.

   **Resolution.** The portal/island disagreement is cosmetic — same mechanism,
   different bookkeeping. The portal-pool framing (one reusable widget, not
   per-table widgets) is the right discipline.

---

## Recommendations — concrete next-step plan

### Stage 0: ~1 hour — verify the headline finding

Before any code change, **measure** whether suspended screens are skipped by
the compositor walk. A focused micro-experiment:

- Build a throwaway app with two screens, each containing ~200 `Static`
  widgets.
- Push screen B so A suspends. Instrument a `Pilot.pause()` or wrap the
  compositor's per-tick render so you can count widgets actually rendered.
- Confirm the count corresponds to B only, not A+B.

This is a one-or-zero outcome that determines whether Stage 3 is even
relevant.

### Stage 1: ~1–2 days — refactor under the cache (P8 + structural map)

**Goal.** Decouple the LRU's contents from the carrier's identity. No user-
visible behaviour change.

- Introduce a `RenderedDocument` (or extend `FileView`) carrying:
  - `strips: list[Strip]` (today's `FileView.lines`-derived strips for flat;
    populated lazily for structural at first)
  - `structural_map: list[(line_start, line_end, kind, payload)]`
  - `match_lines: set[int]`
- Migrate `_flat_buffer_cache` to hold `RenderedDocument` instances rather
  than widget instances. The `LineBufferPreview` widget becomes an
  application-side carrier reading the cached document.
- Build the structural-map alongside `build_file_view`. Cost: one extra walk
  of the chunk list per build.

**Why first.** Every subsequent stage benefits; nothing else regresses.

### Stage 2: ~0.5–1 day — push structural encoding off-thread (P7)

**Goal.** Match the flat path's prefetch model.

- Move `FNDMarkdown` source preparation (`_legacy_blocks_to_md`, etc.) and
  any markdown-it / Rich pre-parsing that can happen pre-mount into the
  existing prefetch worker (`_prefetch_one` in `app.py:2157`).
- On the main thread, prefetch handoff becomes a single `app.mount(widget)`
  per chunk rather than `FNDMarkdown(...)` + mount.

**Why second.** Almost certainly worth doing on its own. Stage 3 needs it to
make screen-prebuilding feasible.

### Stage 3: ~2–3 days — spike P1 (screen-per-file LRU)

**Goal.** Make active-screen DOM independent of `LRU_CAP`.

- Replace `PreviewCache`-of-`PreviewContainer` with `PreviewCache`-of-
  `FilePreviewScreen` (a `Screen` subclass). One mode (`App.add_mode`) per
  pane the user can navigate within.
- Result-list → preview activation calls `app.switch_screen(name)` rather
  than mounting / class-toggling a sibling container.
- Cross-screen updates (live query rerun, focused-chunk band change) route
  through `app.get_screen(name).post_message(...)`.
- Drop any `query_one` calls that previously walked across cached preview
  containers (they would silently fail on suspended screens).

**Decision gate.** Measure after this stage:

- `pilot.pause()` median + p95 under your stress scenarios.
- Click-to-paint latency on a 100% LRU cache hit.
- DOM widget count on the active screen at steady state.

If `pilot.pause()` is back inside the 25/50 ms budget at `LRU_CAP = 16+`, you
can stop here.

### Stage 4 (conditional): ~1–2 weeks — two-tier focused/flat for structural (P3)

Take only if Stage 3 doesn't move the needle enough, or if the user-perceived
mount cost on first-visit-per-file is still too high.

- Build the markdown flattener (`build_file_view`-equivalent for `body_md`
  chunks). The hard part is producing line-accurate `Strip`s from markdown
  source — your existing `FNDMarkdown` block tree gives you the structural
  AST; you'd Console-render each block to Segments and split on newlines.
- Per-file preview becomes a small `Vertical` with three children: pre-flat,
  focused `FNDMarkdown`, post-flat.
- Reuse `MatchAwareScrollBar` against a shared `match_lines` set.

**Decision gate.** Same as Stage 3.

### Stage 5 (last resort): months — single carrier + table portal pool (P2/island)

Only if Stages 1–4 still leave you with unacceptable tail latency at very
large `LRU_CAP` or 1000-page-document workloads. Order of complexity is high
enough that it shouldn't be the first thing you reach for.

---

## Risks specific to fnd's current code

- **Highlight-aware block subclasses (`FNDMarkdownH1`…`FNDMarkdownTD`)** are
  the part of the structural pipeline that doesn't survive a move to a flat
  carrier as-is. The highlight logic itself is portable (`_build_match_spans`
  is already pure), but the *mechanism* changes from "subclass `MarkdownBlock`
  and override `build_from_token`" to "post-process Strip segments". Plan
  rewrite time accordingly.
- **`_finalize_pre_reveal` + the multi-phase mount choreography** (`app.py:
  1761`, `app.py:2453`) is a careful piece of timing logic that exists because
  mounting `FNDMarkdown` widgets is expensive. Screens-as-LRU largely
  eliminates the *reason* for this logic, but it'll need to be re-derived if
  you re-introduce mounting elsewhere (e.g. in Stage 4's two-tier focused
  swap).
- **Match-scrollbar ticks** are line-precise on the flat path but chunk-uniform
  on the structural path (`preview_scrollbar.py:20-32`). Any restructuring
  should converge both to line-precise via the structural map.
- **`_BACKGROUND_FILL_RADIUS = 200`** is a cap that exists because of the
  current mount-cost model. If you go down the screen-per-file path, you can
  almost certainly raise or remove it — but only after measuring.

---

## What I'd not bother with

- **Trying to patch `display: none` to actually skip the compositor.** The
  framework doesn't expose this in a stable way; all three responses agree.
- **Caching `Console.render` bytes and blitting directly.** Breaks selection,
  ticks, and live highlights — Claude 4.7 calls this out explicitly and is
  right.
- **Filing a Textual issue for `Markdown` to mount fewer widgets.** Worth
  filing for posterity, but not a strategy you can ship behind.
- **A full SumTree-style structural index.** Overkill given previews are
  immutable post-decode; a `SortedList` of `(line_start, line_end, kind,
  payload)` plus a `set[int]` of match-bearing lines covers 95% of the value at
  a small fraction of the complexity.

---

*Generated 2026-05-15 from `# GPT & Gemini Responses - DOM.md` plus a read of
`fnd/tui/app.py`, `fnd/tui/line_buffer.py`, `fnd/tui/preview_dispatcher.py`,
`fnd/tui/preview_scrollbar.py`, and `fnd/render.py`.*
