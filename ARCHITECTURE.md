# Architecture

`fnd` is a local document search tool: an indexing pipeline feeding a
Tantivy index, a layered query engine over it, and a Textual TUI for
interactive search with live previews.

## Layers

```text
extract  →  index  →  query  →  TUI
```

**Extract** (`fnd/extract/`). Per-format extractors (`pdf`, `docx`,
`pptx`, `markdown`, `plain`) convert documents into block/chunk
structures. PDF extraction runs a tiered recovery pipeline
(`extract/recovery/`): a chain of `ExtractionTier`s
(`ProductionLayoutTier` → `LigatureRepairTier` → `InvisibleTextTier` →
`FlatFallbackTier` → `DoclingTableTier`) folds each page through
progressively more aggressive repairs, gated by injected quality
evaluators (`CoverageEvaluator`, `LegibilityEvaluator`). Expensive PDF
structure work is cached content-addressed in `fnd/cache.py`.

**Index** (`fnd/index.py`, `fnd/index_runner.py`, `fnd/schema.py`,
`fnd/walk.py`). `walk.py` resolves sources to files; `index_runner.py`
is the async indexer (per-file extraction off-loop, progress events
over an `AsyncIterator`, atomic resume state); `schema.py` is the
single source of truth for Tantivy fields and the schema version.

**Query** (`fnd/query*.py`, `fnd/cascade.py`, `fnd/fusion.py`,
`fnd/layered.py`). User text is validated (`query_plan`), parsed to a
typed AST (`query_dsl` → `query_ast`), and lowered to Tantivy queries
by a visitor (`query_compile`). `layered.py` is the shared entry point
for the CLI and TUI: it picks between the sequential widening cascade
(`cascade.py`: literal → fuzzy → synonym passes) and reciprocal-rank
fusion (`fusion.py`), reranks via `rerank.py`, and emits a trace
(`explain.py`). `matching.py` carries the `MatchSpec` that keeps
highlight semantics identical to search semantics.

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
└── ProgressFacility   fnd/tui/progress.py            preview progress sessions
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

## Concurrency rules

| Owner | Task / primitive | Cancelled by |
|---|---|---|
| `PreviewPresenter` | mount worker (`preview-load`, exclusive), debounce timer, in-flight coalescing latch | file switch / query change (`cancel_mount_task`, latch drop) |
| `LazyMounter` | scroll-driven mount task + debounce timer | file switch / query change (`cancel`) |
| `PrefetchEngine` | decode pool (`preview-prefetch`, exclusive), sink queue + drainer task | stale-query signature checks; user mount preempts |
| `IndexerService` | reindex task, cancel `Event`, event `Queue`, run-generation counter | explicit cancel; a superseded run's teardown is gated by `run_seq` |

Invariants: one scroll anchor at a time (arm → reconcile → release);
a new query drops every preview cache and in-flight task before the
search result lands; chain continuations re-enter through
`app.start_indexer` and inherit the current run generation.

## Module map

```text
fnd/
├── cli.py              CLI commands
├── config.py           Pydantic config (collections, sources, defaults)
├── extract/            per-format extraction + recovery tiers
├── index*.py, walk.py  index building
├── query*.py           parse → validate → AST → compile
├── cascade.py, fusion.py, layered.py, rerank.py   search passes + ranking
├── matching.py, render.py                          match semantics + rendering
└── tui/
    ├── app.py          FNDApp composition root
    ├── search_controller.py, results_view.py, scope_panel.py, indexer_service.py
    ├── preview/        presenter, flat_view, prefetch, lazy_mount, tuning
    ├── preview_scroll.py, preview_scrollbar.py, line_buffer.py
    ├── widgets/        markdown, results_tree, preview_container, …
    └── settings_screen.py, indexer_modal.py, menu.py   screens
```
