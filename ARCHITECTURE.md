# Architecture

`fnd` is a local document search tool: an indexing pipeline feeding a
Tantivy index, a layered query engine over it, and a Textual TUI for
interactive search with live previews.

## Layers

```text
extract  →  index  →  query  →  TUI
```

**Extract** (`fnd/extract/`). Per-format extractors (`pdf`, `docx`,
`pptx`, `markdown`, `plain`, `epub`, `code`, `data`, `web`, `notebook`,
`odf`) convert documents into block/chunk structures. PDF extraction
runs a tiered recovery pipeline (`extract/recovery/`): a chain of
`ExtractionTier`s (`ProductionLayoutTier` → `LigatureRepairTier` →
`InvisibleTextTier` → `FlatFallbackTier` → `DoclingTableTier`) folds
each page through progressively more aggressive repairs, gated by
injected quality evaluators (`CoverageEvaluator`,
`LegibilityEvaluator`). Expensive PDF structure work is cached
content-addressed in `fnd/cache.py`, and runs in a subprocess so a
wedged parse can't take the app with it.

Which formats exist at all is *not* decided here. `fnd/kinds.py` is the
single registry: one `KindSpec` per format binding a stable `id` (the
stored `F_KIND` value), its suffixes, its extractor module, and the
display `Category` it groups under. The walker, extraction dispatch,
preview router, config/apps validation, CLI and Filters tree all derive
their lookups from it, so a new file type is one row plus an extractor.
Categories are a grouping concept for the UI only — never stored; a
category filter expands to its member kind ids.

**Index** (`fnd/index.py`, `fnd/index_runner.py`, `fnd/schema.py`,
`fnd/walk.py`). `walk.py` resolves sources to files; `index_runner.py`
is the async indexer; `schema.py` is the single source of truth for
Tantivy fields and the schema version (currently 9 — a bump requires a
reindex, gated by the `.fnd-schema-version` sidecar).

A run has two phases, and both must stay answerable to the user:

- **Scan.** The walk is pumped in short time-boxed slices rather than
  one opaque thread hop, so cancel is honoured mid-scan and each slice
  emits an `enumerating` event carrying the running file count. This
  matters because a scan is not always fast: a source with a
  `frontmatter_filter` must open every candidate to evaluate it, and on
  cloud-backed storage each open blocks on a download.
- **Per-file.** Extraction runs off-loop in `asyncio.to_thread`, with
  progress events over an `AsyncIterator` and atomic resume state
  written after every file.

By default, cloud-only files are fetched rather than refused, so an
Update produces a complete index. `CloudPolicy` bounds each fetch
(`defaults.cloud_fetch_timeout_s`) and publishes what it is waiting on,
so the wait reads as work rather than a hang. It also carries the user's
live "skip cloud-only files" opt-out; once that is set, the run trades
completeness for speed and reports what it left behind. Either way a file
is never dropped silently — one the scan couldn't resolve is counted and
logged alongside extraction failures, even though it never reaches the
per-file loop.

**Query** (`fnd/query*.py`, `fnd/cascade.py`, `fnd/fusion.py`,
`fnd/layered.py`). User text is validated (`query_plan`), parsed to a
typed AST (`query_dsl` → `query_ast`), and lowered to Tantivy queries
by a visitor (`query_compile`). `layered.py` is the shared entry point
for the CLI and TUI: it picks between the sequential widening cascade
(`cascade.py`: literal → fuzzy → synonym passes) and reciprocal-rank
fusion (`fusion.py`), reranks via `rerank.py`, and emits a trace
(`explain.py`). `matching.py` carries the `MatchSpec` that keeps
highlight semantics identical to search semantics.

Scope (which collections / sources are live) is owned by
`ScopeController` and persisted to `state/scope.toml`. That saved
selection is authoritative; `defaults.collection` — `all` by default, or
a collection name — only seeds a profile that has never saved one, so the
setting can never fight the sidebar. `-c` overrides scope for one launch,
with `all` as the pseudo-name for every collection (a real collection of
that name still wins, and new ones can't take it).

**Filters** (`fnd/query_filters.py`, `fnd/filter_dsl.py`,
`fnd/tags.py`, `fnd/tag_query.py`, `fnd/tag_catalog.py`,
`fnd/fsmeta.py`, `fnd/kind_catalog.py`). Scope selections from the TUI
panel and `--kind` / `--tag` / `--created` / `--modified` on the CLI
compile to the same hard Tantivy filters that wrap the ranked query.
Tags carry provenance in separate fields (`F_TAGS_FM` frontmatter,
`F_TAGS_OS` Finder), so switching a source off takes effect without a
reindex. `fsmeta.py` reads creation and change times per platform.

## Platform seams

Everything OS-specific lives behind four modules, so feature code never
branches on `sys.platform`:

| Seam | Question it answers |
|---|---|
| `fnd/paths.py` | Where do config, index, cache and state live? |
| `fnd/launcher.py` | How do I open a path/URL, and reveal a file in the file manager? |
| `fnd/os_labels.py` | What does this OS *call* things (Finder vs File Explorer, ⌥ vs Alt)? |
| `fnd/cloud_files.py` | Are this file's bytes local, or will touching it pull them over the network? |

Each resolves the platform in one place, and every branch is reachable
from a test on any host: `launcher.py` takes its process runner (and
Windows' `os.startfile`) as injected dependencies, `os_labels.py` stays
deliberately uncached so a test can just repoint `platform.system`, and
`cloud_files.py` reads the placeholder bit through `os.stat`. Deep-linking
into a specific app is deliberately *not* a seam concern — that is
per-app, owned by `fnd/apps.py` handlers and `fnd/opener.py` dispatch.

macOS is the tested platform; the Linux and Windows arms of these seams
are early beta (see the README).

## TUI composition

`FNDApp` (`fnd/tui/app.py`) is a composition root: layout (`compose`),
bindings and action dispatch, focus chrome, and the wiring of the
components that own the actual state. Each component takes the app
reference and exposes a plain public API.

```text
FNDApp
├── SearchController   fnd/tui/search_controller.py   query → results
├── ResultsView        fnd/tui/results_view.py        results tree rendering
├── ScopeController    fnd/tui/scope_panel.py         collections/sources/filters + persistence
├── IndexerService     fnd/tui/indexer_service.py     background reindex task + chains
├── PreviewPresenter   fnd/tui/preview/presenter.py   structural preview core
├── FlatBufferView     fnd/tui/preview/flat_view.py   flat (line-buffer) preview path
├── PrefetchEngine     fnd/tui/preview/prefetch.py    background warming
├── LazyMounter        fnd/tui/preview/lazy_mount.py  scroll-driven mounting
├── PreviewScrollController  fnd/tui/preview_scroll.py  scroll positioning
└── ProgressFacility   fnd/tui/progress/              the progress line
```

Textual-specific surfaces stay on the app: `@on` message handlers and
`action_*` methods are bound to the App class and delegate one line
into the owning component. Widgets live under `fnd/tui/widgets/`
(highlight-aware Markdown tree, results tree, preview containers);
screens (`settings_screen.py`, `indexer_modal.py`, …) are Textual
Screens that read the components through the app.

## The preview pipeline

A cursor move debounces into `PreviewPresenter.schedule_load`. A worker
thread decodes the file's chunks; the mount path then splits by
`preview_dispatcher.choose_preview_mode`:

- **Flat** (PDF/TXT): `FlatBufferView` installs a prebuilt
  `RenderedDocument` into one shared `LineBufferPreview` widget and
  scrolls by line.
- **Structural** (md/docx/pptx): chunks mount as widgets inside a
  per-file `PreviewContainer` — focused window first (instant
  feedback), background fill bounded by radius, then `LazyMounter`
  extends the mounted region as the user scrolls. `prune_active_to_window`
  keeps the DOM small.

Scroll-to-match is reconciled by `PreviewScrollController`: navigation
*arms* a single anchor; mount/finalize events *reconcile* against it
(idempotent), and a strategy (`StructuralScrollStrategy` /
`FlatScrollStrategy`, host = `PreviewPresenter`) performs the actual
scroll once layout has settled. The incoming container builds at
`opacity: 0` and is revealed only after its scroll commits, so file
switches never flash an unscrolled page. `PrefetchEngine` warms the
next results through a single-consumer sink queue that always yields
to the user-side mount.

Mount-window tunables live in `fnd/tui/preview/tuning.py` and are read
at call time.

## The progress line

One row under the panes, blank at rest, driven by `fnd/tui/progress/`.
An operation opens a session against an `OperationPlan` — an ordered set
of phases, each with an expected duration. Phases with real units report
them; phases with nothing to count (a single `await build_done`, a layout
settle) ease on elapsed time. A phase's **weight is its share of the
plan's total expected duration**, so `calibration` — which records what
each phase actually cost and summarises the recent runs, the same shape
as `cost_estimate.py` — reshapes the bar without any hand-tuned numbers.

Sessions are **observed, not reported**. `PreviewProgressTracker` reads
the preview pipeline's own signals (`pipeline_busy()`, the mount window's
`mounted_indices`, `inflight_target`, `is_settling`); `IndexProgressTracker`
reads `IndexerService.state` rather than the event queue, which has a
single consumer in the modal. The mount path therefore has no progress
calls to keep in step, and no stale exit can strand or steal the line.

Adding a subsystem means adding a plan and a tracker satisfying
`ProgressTracker` — nothing else knows about it. Each tracker translates
its own units (rendered lines, mounted chunks, indexed files) into
`report(done, total)` at the boundary, and the phase weights turn the
rest into one 0..1 fraction; that normalisation is what lets operations
with no unit in common share a line.

A plan also declares its `OperationKind`. INTERACTIVE work answers
something the user just did and always owns the line; AMBIENT work — a
background reindex — is *suspended* while that happens and resumes
afterwards, so a run spanning hundreds of navigations is not retired by
the first one. Since only one can be on screen at a time, ambient is
also the only class that carries a label, and it paints in a dimmer
accent: a line that appears without the user touching anything reads
differently from one that answers a keypress. Its stall backstop is
correspondingly looser, because its terminator (`task.done()`) is a real
result rather than an inference.

Queries deliberately have no session: a query is debounced typing, so a
line would appear and clear on nearly every keystroke. Search runs off
the loop instead, which is the reassurance a query actually needs.

Sessions are owned: closing one that has already been superseded does
nothing. Visibility is policy, not caller choice — a session paints on
the frame it opens, holds a minimum visible duration, always eases to a
full line before clearing, and hands its fill to a successor so a held
cursor key doesn't saw the bar back to zero. Fast work is shown, not
suppressed: a load the user can see complete is what makes the app feel
fast.

## Concurrency rules

| Owner | Task / primitive | Cancelled by |
|---|---|---|
| `SearchController` | search worker (`search`, exclusive, thread) | a newer query — but Textual only *marks* a thread worker cancelled, so the stale search still runs to completion and is discarded by the generation guard in `_commit` |
| `PreviewPresenter` | mount worker (`preview-load`, exclusive), debounce timer, in-flight coalescing latch | file switch / query change (`cancel_mount_task`, latch drop) |
| `LazyMounter` | scroll-driven mount task + debounce timer | file switch / query change (`cancel`) |
| `PrefetchEngine` | decode pool (`preview-prefetch`, exclusive), sink queue + drainer task | stale-query signature checks; user mount preempts |
| `IndexerService` | reindex task, cancel + skip-cloud `Event`s, event `Queue`, run-generation counter | explicit cancel; a superseded run's teardown is gated by `run_seq` |

Auto-resume (`IndexerService.maybe_resume`, opt-in) considers every
`*.state.toml`, resumes the most recent and chains the rest, and sweeps
states that can never be resumed (collection deleted, run finished). It
is the one path that starts indexing without the user asking, so the
opt-in gate is checked immediately before starting — never before the
sweep, which should tidy either way.

Invariants: one scroll anchor at a time (arm → reconcile → release);
a new query drops every preview cache and in-flight task before the
search result lands; chain continuations re-enter through
`app.start_indexer` and inherit the current run generation. The event
`Queue` and the skip-cloud `Event` are reused across a chain's steps —
the modal's drain holds one queue reference, and a mid-chain opt-out
should stay opted out — while a fresh run allocates both.

## Module map

```text
fnd/
├── cli.py              CLI commands
├── config.py           Pydantic config (collections, sources, defaults)
├── kinds.py, kind_catalog.py    file-type registry + Filters grouping
├── extract/            per-format extraction + recovery tiers
├── index*.py, walk.py, schema.py, migrate.py    index building + schema
├── query*.py           parse → validate → AST → compile
├── cascade.py, fusion.py, layered.py, rerank.py   search passes + ranking
├── matching.py, render.py, display_text.py        match semantics + rendering
├── tags.py, tag_query.py, tag_catalog.py, filter_dsl.py, fsmeta.py   filters
├── paths.py, launcher.py, os_labels.py, cloud_files.py   platform seams
├── apps.py, opener.py, launch_command.py          open-in-app + shareable commands
├── cache.py, seen_log.py, texture_maintenance.py  extraction cache + upkeep
└── tui/
    ├── app.py          FNDApp composition root
    ├── search_controller.py, results_view.py, scope_panel.py, indexer_service.py
    ├── preview/        presenter, flat_view, prefetch, lazy_mount, tuning
    ├── preview_scroll.py, preview_scrollbar.py, line_buffer.py
    ├── widgets/        markdown, results_tree, preview_container, toggle_tree, …
    └── settings_screen.py, indexer_modal.py, menu.py   screens
```
