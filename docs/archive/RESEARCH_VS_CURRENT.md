# Preview pane — research recommendation vs current implementation

This document compares the prior Perplexity research (attached file
`3443c20d-Perplexity_Report__Im_building_a_Textual_Python_TUI_fulltext_sear.md`)
against the implementation as it stands on the
`investigation/preview-perf-2026-05-14` branch. The research was done
before pre-load, W3 DataTable, and the cache-LRU work landed; revisiting
it now reveals that the bottleneck the current click-latency problem
maps directly to a constraint the research explicitly called out.

The document closes with a section that succinctly states the
remaining problem and a self-contained prompt that can be handed to an
AI with research capabilities to seek further design alternatives.

---

## 1. What the research recommended

Headline ranked recommendation: **a single `ScrollView` subclass with
a `List[Text]` line buffer per file** (`LineBufferPreview`), used for
all "plain text" inputs (PDF, TXT). Structured formats (md / docx /
pptx) keep the existing structural renderer because their AST-driven
rendering — headings, tables, code fences, lists — benefits from the
per-block widget tree.

Core properties of the line-buffer design:

1. **One widget in the DOM per file**, regardless of file length.
   Rendering goes via Textual's line API (`render_line(y)` +
   `line_count`); only visible viewport rows are painted, exactly the
   pattern Textual's own `TextArea` / `Log` / `RichLog` / `DataTable`
   use.
2. **Per-line spans for match highlights and chunk bands** are baked
   into Rich `Text` objects at build time — no separate widgets per
   span, per line, or per chunk.
3. **Precise `scroll_to_line(line_index)`** maps chunk + relative
   offset to global line index. Match-scrollbar markers driven by the
   same line-index → fractional-position map via a custom
   `ScrollBarRender`.
4. **Multi-line selection / copy** via `get_selection(selection)`
   mirroring `Log.get_selection` — single bounding-box over the
   widget's internal buffer.
5. **Per-file lazy mount cost** is just decode + Rich `Text`
   allocation; no widget mounting cost grows with file length. Cross-
   file revisit is constant-DOM regardless of how many files have
   been opened.

Key constraint the research emphasised: **DOM size on every keystroke
is the bottleneck**. Per-line or per-block widgets cause Textual's
compositor walk and event-dispatch costs to scale with file length.
The single-widget line-buffer eliminates that.

The research also recommends:

- **Hybrid renderer dispatch**: structural for md / docx / pptx,
  line-buffer for PDF / TXT.
- **Parallel decode** via `concurrent.futures.ThreadPoolExecutor`
  using `tantivy.Searcher` (GIL is released in `Searcher.doc()` on
  recent tantivy-py).
- **Adaptive chunking** aimed at ~200 chunks per file with a 40–400
  line clamp, so the per-file chunk count stays usable.

---

## 2. What the current implementation looks like

### 2.1 Flat (plain-text) path — already matches the research

`fnd/tui/line_buffer.py` (687 lines) implements `LineBufferPreview`
exactly as recommended:

- Subclass of `ScrollView`.
- Internal `list[Strip]` strip buffer + `visual_to_logical` /
  `logical_to_visual_start` maps.
- `render_line(y)` returns the cached strip for viewport row `y`.
- `scroll_to_chunk(focus_chunk_seq, prefer_first_match=True)` resolves
  to a logical line, then a visual y, then `scroll_to(y=…)`.
- Match-aware scrollbar via `MatchAwareScrollBar` in
  `fnd/tui/preview_scrollbar.py`.
- Multi-line selection (`ALLOW_SELECT = True`).
- Pre-rendered Rich `Text` lines with match spans baked in.

PDF and TXT use this path. The match-marker scrollbar, focused-chunk
band, and chunk-boundary gaps all live here as documented in the
research.

### 2.2 Structural path (md / docx / pptx) — diverges from the research

`fnd/tui/app.py` handles md / docx / pptx via:

- **One `PreviewContainer`** per file.
- **One `FNDMarkdown` widget per chunk**, where each chunk renders
  via Textual's `Markdown` AST (headings, paragraphs, code fences,
  lists, tables).
- Each `FNDMarkdown` instance owns its own per-block widget tree
  (`MarkdownParagraph`, `MarkdownHeading`, `MarkdownFence`,
  `MarkdownTable`, etc.).
- W3 DataTable (`FNDMarkdownTableDT`, `commit bbc3001`) collapsed
  the old widget-per-cell table render (~50 widgets per table) to a
  single `DataTable` per table — a partial mitigation, not a
  fundamental change.

A "heavy" md chunk produces ~30–50 sub-widgets. A 30-chunk file is
~1000 widgets in the DOM the moment it's fully mounted.

### 2.3 Cache + prefetch — built on top of the structural path

A cross-file LRU caches mounted `PreviewContainer` instances so revisit
is a class-toggle, not a re-mount. Current settings on this branch
(see `_PREVIEW_CACHE_MAX_FILES`, `_BACKGROUND_FILL_RADIUS` removed,
prefetch loop):

- `_PREVIEW_CACHE_MAX_FILES = 16`
- Prefetch mounts the **whole file** for each prefetched target
  (recent change; the prior default was focused-chunk-only).
- Cold-mount on user click does Phase 1a (focused chunk first) +
  Phase 1b (visible window) + Phase 2a/2b (background fill to start
  and end of file).
- Cursor-following prefetch re-anchors around the current cursor on
  every settled cursor move.

The result: cached files in steady state hold a fully-mounted widget
tree each, hidden via `display: none` between activations. Empirical
measurement from `tests/perf/bench_input_lag.py` (5 cached files
heavy-md, branch HEAD `0850012`):

| Metric | W3 baseline (Aug 14) | Current branch |
|---|---|---|
| DOM (preview pane) | ~130 widgets | **793 widgets (5 cached files)** |
| pilot.pause median (idle) | 24 ms | **44 ms** |
| pilot.pause p95 (during click) | <50 ms | **1083 ms** |
| pilot.pause max | <50 ms | **1124 ms** |
| Symptom-harness steady-state click latency | n/a | **~0.85 s** synthetic |
| User-reported real-corpus click latency | n/a | **3–6 s** |

Extrapolated to LRU=16 fully populated: ~2500 widgets, well above
W3-era's bloat zone (~3000 widgets pre-W3, the original "everything
lags" state).

### 2.4 What's been resolved already

The current branch *does* fix several real bugs surfaced by the
investigation harness:

- Title-not-updating after structural cache hit (`ba14fb3`).
- Cold-mount `first_match_block` resolution rewired to
  `await md_widget.lock` (`0850012`) — no more 30-retry fallback to
  chunk-top on heavy md files, so "first load doesn't show the match"
  is addressed.
- Pre-mount cancellation properly drains the sink queue across
  cursor moves; prefetch_top filter uses `_preview_cache` rather
  than `_chunk_cache` so files whose mount got drained re-queue.
- Diag log entries are timestamped (monotonic seconds), and
  cache_check dumps the full set of `cache_keys` / `dom_keys` for
  post-hoc analysis.
- New harness `tests/perf/bench_user_symptoms.py` measures per-click
  wall-clock to title-update / focused-widget-mounted / first-match-
  resolved / widget-visible / scroll-completion.

What remains is the steady-state cache-hit click latency, and that
appears to be a structural consequence of the per-chunk-widget design
for structured formats.

---

## 3. Comparison summary

| Concern | Research recommendation | Current state |
|---|---|---|
| PDF / TXT preview | `LineBufferPreview(ScrollView)` with `list[Text]` buffer | **Matches.** Implemented in `fnd/tui/line_buffer.py`. |
| md / docx / pptx preview | Structural per-chunk renderer "because format matters" | **Matches in shape.** Per-chunk `FNDMarkdown` widgets via Textual `Markdown`. |
| Tables inside md | Per-cell widgets via Textual's `MarkdownTable` | **Improved.** W3 swaps to a single `DataTable` per table. |
| Match highlights | Baked into Rich spans on the line buffer | Flat path: ✓. Structural: per-block via `_apply_highlights_after_build`, **stored as Content spans on individual widgets** in the per-chunk tree. |
| Cross-file caching | "Single widget per file means revisits are constant DOM regardless of cache size" | **Diverges.** LRU caches the widget tree for each visited file. DOM grows with cache. |
| Pre-mount / prefetch | Not in the research scope (prefetch was added after) | **Added.** Cursor-following pre-mount of N files; W3 enabled default-on. |
| Scrollbar match markers | Custom `ScrollBarRender` driven by `match_lines` set | **Matches** for the flat path (`MatchAwareScrollBar`). Structural path has no per-line scrollbar markers. |
| Multi-line selection | `get_selection` on the line buffer | **Flat path only.** Structural per-chunk widgets don't support cross-chunk selection. |
| Parallel decode | `ThreadPoolExecutor` over `tantivy.Searcher.doc()` | **Matches.** `preview_decode_workers` bounds the pool size. |
| Adaptive chunking | ~200 chunks per file, 40–400 lines | **Different.** Current chunking is structure-driven; per-file chunk count varies widely. |

The clear mismatch: **the line-buffer design says one widget per file**
to keep DOM constant. The current structural path keeps a widget tree
per cached file, multiplied by the LRU cap. Pre-mount and W3 are both
attempts to make that bloat survivable, but the fundamental constraint
the research called out is still binding.

---

## 4. The remaining issue, succinctly

**Problem.** Heavy markdown files (and the prefetched cache that
exists to make their click-to-load instant) blow up Textual's DOM to
hundreds-to-thousands of widgets, which scales every refresh tick
roughly linearly with widget count. Steady-state click latency on the
user's real corpus is 3–6 seconds, even though the focused chunk's
content is already mounted; the time is spent waiting for Textual's
compositor to redraw with the new content visible.

The W3 DataTable change (one widget per table instead of ~50) was the
right shape but didn't go far enough; the per-paragraph,
per-heading, per-code-fence widgets that remain still dominate. The
research's recommendation to flatten *everything* into a single-widget
line buffer would solve it, but it sacrifices interactive markdown
behaviour (table scrolling, focused-code-fence interactions, link
clicks).

**What we have*** addresses correctness (title-refresh, first-match
visible, cursor-following prefetch). What it does *not* address is
that the DOM-walk cost on every refresh tick is structural, and
shrinking it requires either:

- Mounting fewer per-file widgets in the structural path (e.g. flatten
  markdown into a Rich-styled line buffer the way the flat path does,
  per the research's hybrid suggestion taken further), **or**
- Not keeping non-active cached files in the DOM at all (the LRU keeps
  Python objects alive but unmounts them from `#preview_pane`,
  re-mounting on activation), **or**
- A different containment model entirely that the research did not
  consider — exactly the alternative space this document's research
  prompt is for.

---

## 5. Research prompt — alternatives to investigate

Use this prompt verbatim with an AI research tool (Perplexity, Claude
Research, Gemini Deep Research, etc.). It outlines the load-bearing
functionality the current implementation must preserve, alongside the
DOM-size + responsiveness constraints, so the search can focus on
genuinely new design space rather than rediscovering choices we've
already evaluated.

```
I'm building "fnd" — a Textual-based Python TUI full-text search
tool over heterogeneous local document corpora (PDF, TXT, Markdown,
DOCX, PPTX). Search hits resolve to chunks; users navigate hits with
the arrow keys; the preview pane shows the matched file scrolled to
the matched chunk, with character-level highlight spans on the actual
match, a focused-chunk visual band, and tick-marker positions on the
scrollbar for every line that contains a match.

I want to investigate preview-pane rendering strategies for the
structural formats (md / docx / pptx) — the plain-text formats (PDF
/ TXT) already use a single-widget virtualised line buffer pattern
that works well. Please return DESIGN SPACE, not implementation: I'm
looking for patterns I haven't already considered, evidence for or
against each, and the trade-offs against the constraints below. Do
NOT recommend reverting interactivity; the goal is to keep ALL of
the functionality below while removing the DOM blow-up.

Hard functional constraints (must all be preserved):

  1. Scroll to a specific chunk and to the matched line within that
     chunk.
  2. Per-line / per-character match highlights with three style
     variants: exact-literal match, fuzzy match (highlighted char by
     char where alignment differs), focused-chunk band.
  3. Visible separator / gap between chunks.
  4. Match-position tick markers on the scrollbar, accurate to true
     line position (not chunk-uniform).
  5. Sidebar's "page N of M" / chunk metadata — preview-renderer-
     independent.
  6. Cross-file LRU cache so revisits are instant (no re-decode, no
     re-render).
  7. Cursor-following prefetch buffer — the next N files in the
     result list are pre-decoded and pre-rendered ahead of the user's
     navigation, so navigating to them is single-frame.
  8. Multi-line text selection and clipboard copy from the preview.
  9. Markdown semantic rendering: headings (per-level styling),
     paragraphs, ordered/unordered lists, blockquotes, inline code,
     inline emphasis, **fenced code blocks with syntax highlighting**
     (current implementation uses rich.syntax.Syntax for those),
     tables (currently rendered as Textual DataTable per markdown
     table so the user can scroll wide tables), and reasonable
     fallbacks for links.
 10. Live query re-runs while a preview is open must update highlights
     without re-decoding or re-rendering the document — only the
     match spans change.
 11. Reasonable performance on documents up to ~1000 pages
     (PDF/DOCX/PPTX text layer) and up to ~100k lines for plain text.

Performance constraints:

  A. Steady-state cache-hit click latency must be <100 ms perceived
     (current real-corpus measurement: 3–6 seconds).
  B. Textual `pilot.pause()` median must stay <25 ms, max <50 ms,
     because anything above that is felt as input lag.
  C. The cross-file LRU cache must not cause the DOM widget count to
     scale with cache size in a way that breaks (B). Empirically, the
     pre-W3 DOM of ~3000 widgets made the app unusable; ~130 widgets
     was responsive.
  D. Preview pre-mount must not block the event loop or starve
     keystroke handling.

What we've already tried (don't re-suggest these):

  - One Textual Markdown widget per chunk, with per-block sub-widgets
    (current default). DOM blows up linearly with chunk count.
  - W3: collapse markdown tables to a single Textual DataTable
    instead of widget-per-cell. Necessary but not sufficient.
  - Pre-mount prefetch with `_PREFETCH_MOUNT_RADIUS=0` (only the
    focused chunk per cached file): keeps DOM small but loses the
    "in-file navigation to next match is free" property.
  - Pre-mount prefetch with full file mount: gives the "free in-file
    nav" property but DOM grows ~30-50 widgets × LRU cap files.
  - Flat-render markdown via `rich.markdown.Markdown` to styled
    lines, feed into the existing line buffer (W8 / `_md_flat.py`):
    fast but drops per-heading CSS, table scrolling, code-fence
    interactivity, link clicks.
  - Hybrid: a single chunk widget that yields a flat list of
    text widgets PLUS embedded interactive islands for tables and
    code fences (`_md_hybrid.py`, opt-in): partial; degrades visual
    polish.

Specific questions to investigate:

  Q1. Is there a Textual-native pattern for "mount a widget tree
      once, then DETACH it from the DOM while keeping its rendered
      strips cached, and re-attach on demand without rebuilding the
      tree"? This would let the LRU cache hold rendering state
      without paying the per-tick DOM walk cost on cached-but-
      inactive files.

  Q2. Is there prior art (Textual or other TUI frameworks: Bubbletea,
      Ratatui, blessed-contrib, ink) for compositing multiple "fully
      formatted" rendered documents in a way that one widget hosts
      many documents and only walks the active one on refresh? The
      Frogmouth and Harlequin precedents are well-known; what about
      less-publicised projects (research / scientific note tools,
      enterprise log viewers, mainframe terminal IDE plugins)?

  Q3. Can Textual's `Strip` / `Segment` rendering be driven from a
      single virtualised widget that's TOPOLOGICALLY one widget but
      LOGICALLY a tree of nested blocks (so headings still get their
      per-level CSS via component classes, code fences still get
      syntax highlighting via rich.syntax, tables still get cursor
      navigation), without each block being a Widget instance? For
      example, render the whole file to a list of pre-baked Strips
      and overlay "interactive islands" as floating sub-widgets only
      for the focused chunk's tables / code fences while everything
      else is flat?

  Q4. Are there text-buffer-of-blocks designs from the literature
      (Emacs, neovim, Sublime, VS Code) where the buffer is a flat
      array of styled lines but block-level operations (collapsing,
      folding, focus indicator) are tracked on a parallel structural
      map? How is the structural map kept in sync with the flat
      buffer on edits / re-styling, and what's the per-tick cost?

  Q5. Is there a "ghost mount" pattern — keep the Python object
      alive in the LRU but mounted to a hidden off-screen container
      that Textual's compositor explicitly skips — that I've missed
      in Textual's API? (Note that `display: none` does NOT skip the
      compositor walk in the version we measured.)

  Q6. Adaptive strategy: render the focused chunk via the per-block
      Markdown tree (interactive) and all OTHER chunks of the same
      file via a flat line-buffer; swap on chunk focus change. Has
      this been done? What's the swap latency?

For each idea returned, please cover:

  - The minimum viable implementation cost (in terms of new widget
    classes / API surface).
  - Which of the hard functional constraints (1–11) it preserves or
    sacrifices.
  - Which of the performance constraints (A–D) it improves.
  - Risk of regressing the rendering polish (per-heading CSS,
    syntax-highlighted code, table scrolling, click-to-follow link).
  - Real-world precedents (production apps, open-source projects)
    that demonstrate the idea working, not just the original paper.

I'm not looking for a winner from a list of options; I'm looking for
patterns I haven't considered. Bias toward depth over breadth.
```

---

## 6. What to do with the answers

When the research returns, weigh each candidate strictly against
constraints 1–11 and A–D in §5. The bench in
`tests/perf/bench_user_symptoms.py` plus the timestamped diag log give
us a way to measure any prototype against the W3 baseline (24 ms /
130 widgets) without manual user testing.

The current branch tip is `0850012`. The remaining open issue (task
#56: "Reduce cache-hit click latency on real corpora") is the target
metric for whichever direction the research surfaces.
