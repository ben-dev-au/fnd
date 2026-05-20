# Real PDF Support — Phased Plan

**Companion to:** `docs/specs/2026-05-20-real-pdf-support.md`.
**Branch:** `feat/real-pdf-support` (Phase 0 only).

Phase 0 is the bake-off — we measure before we build. Phases 1-4 are
sketched at one paragraph each; they fill in once Phase 0 picks a
winner and answers the caching question.

## Phase 0 — Bake-off harness *(this PR)*

Build `tools/pdf_bakeoff/` — a CLI that runs candidate PDF extractors
against a folder of PDFs and emits structured results.

**Deliverables**

- `tools/pdf_bakeoff/` Python package with a runnable CLI.
- Six runners: `baseline_pymupdf` (reference), `pymupdf4llm_layout`,
  `pymupdf4llm_legacy`, `docling` (opt-in), `marker` (opt-in),
  `mineru` (opt-in).
- Per-page `metrics.csv`, per-`(pdf, runner)` `summary.csv`,
  per-page side-by-side Markdown outputs for visual diff.
- `RESULTS.md` filled by the harness from `RESULTS_TEMPLATE.md` with
  aggregate numbers and a blank human-scoring table.
- `tests/fixtures/pdf_bakeoff/README.md` documenting stratification
  categories and sourcing policy.
- `tests/test_pdf_bakeoff.py` shape-only smoke test (green in CI).

**Acceptance criteria**

- `uv run python -m tools.pdf_bakeoff --help` works.
- Runs end-to-end on `tests/fixtures/papers/test.pdf` without
  crashing.
- Runs against `~/Documents/Readings` with `--max-pdfs 20
  --pages-per-pdf 5` in under a few minutes (baseline + both
  pymupdf4llm modes; opt-in extractors excluded by default).
- `make lint && make test` green.
- Zero changes to `fnd/extract/pdf.py`, `fnd/render.py`,
  `fnd/tui/preview_dispatcher.py`, `fnd/schema.py`.

**Out of scope for Phase 0**

- Picking a winning extractor — that's the human's job once
  `RESULTS.md` is filled in against a real corpus.
- Choosing the cache design — see spec "Caching — decisions to make".
- Modifying any production extraction or rendering code.

## Phase 1 — Integrate winning extractor (opt-in extras)

User requirement: PDF formatting must be **opt-in**, with full
disclosure of additional disk + download cost. Default `fnd install`
remains lean; structured PDF rendering is a choice the user makes
explicitly. Uninstall reverts cleanly to the current flat-text
behaviour.

### `fnd extras` CLI

```
fnd extras list                    # show available + installed
fnd extras status                  # disk usage per extra, last touched
fnd extras install pdf-structure   # interactive prompt, then install
fnd extras uninstall pdf-structure # interactive prompt, then remove
```

Install prompt example:
```
$ fnd extras install pdf-structure

This will install structured PDF rendering, which uses two extractors:

  pymupdf4llm 1.27   ~10 MB Python package, no model weights
  docling-slim 2.x   ~500 MB Python package + ~400 MB ML weights

Total disk: ~910 MB
Network:    ~910 MB downloaded once

After install, run `fnd collection reindex <name>` to apply structured
extraction to existing PDFs. New PDFs added later are extracted
structurally by default.

Without this extra, PDFs render as flat text (current behaviour).

Continue? [y/N]
```

Uninstall prompt:
```
$ fnd extras uninstall pdf-structure

This will remove:
  pymupdf4llm     (project venv)
  docling-slim    (uv tool venv, ~500 MB)
  ML weights      (~/Library/Caches/fnd/docling-models/, ~400 MB)

Already-indexed structured chunks remain in the index — previews
keep working. New extractions revert to flat text. To fully revert
existing collections, run `fnd collection reindex <name>` after
uninstall.

Continue? [y/N]
```

### pyproject restructure

`pymupdf4llm~=1.27` moves from hard dependency to optional:

```toml
[project.optional-dependencies]
ocr = ["ocrmypdf~=17.0"]
# Structured PDF rendering — opt-in via `fnd extras install pdf-structure`.
# docling-slim is installed separately via `uv tool install` due to
# transitive version conflicts (typer<0.22 vs fnd's typer~=0.25).
pdf-structure = ["pymupdf4llm~=1.27"]
```

`fnd extras install pdf-structure` runs:
- `uv pip install --upgrade "fnd[pdf-structure]"` (gets pymupdf4llm into the project venv)
- `uv tool install "docling-slim[standard]"` (isolated tool venv for docling)

### Two extraction code paths in `fnd/extract/pdf.py`

Detection at module load:
```python
try:
    import pymupdf4llm
    import shutil
    _HAS_PYMUPDF4LLM = True
    _HAS_DOCLING = shutil.which("docling") is not None
except ImportError:
    _HAS_PYMUPDF4LLM = False
    _HAS_DOCLING = False
```

Dispatch in `extract(path)`:
```python
def extract(path: Path) -> Iterator[Chunk]:
    if _HAS_PYMUPDF4LLM:
        yield from _extract_structured(path)   # body_md populated
    else:
        yield from _extract_flat(path)         # current behaviour
```

`_extract_flat` is **today's `fnd/extract/pdf.py` verbatim** — preserved
in its entirety. Users who don't install the extra get the exact
behaviour they have today.

`_extract_structured` is new: pymupdf4llm primary, docling fallback
per Phase 3 routing logic. Populates `body_md` (the routing signal)
and a richer `body_struct`.

### Preview dispatcher

Add `"pdf"` to `_MARKDOWN_RENDERED_KINDS` in
`fnd/tui/preview_dispatcher.py`. The existing dispatcher rule
`kind in _MARKDOWN_RENDERED_KINDS and body_md` already handles the
fallback: if `body_md` is empty (flat extraction), PDF stays on the
flat preview path automatically. No additional branching needed.

### Reindex behaviour

Schema version stays at 7 — the existing `body_md` field accommodates
both modes. After `fnd extras install`, the user runs
`fnd collection reindex <name>` to re-extract existing PDFs
structurally. New PDFs auto-detect the extras and pick the right
path on first index.

### Acceptance criteria

- `fnd extras install pdf-structure` works end-to-end on a clean
  install; shows the disk-impact prompt; installs both packages.
- `fnd extras uninstall pdf-structure` works end-to-end; removes
  both packages; existing indexed chunks remain.
- Without the extra: `fnd` works exactly as it does today. Zero
  behavioural change for users who don't opt in.
- With the extra: PDF previews show headings, lists, bold/italic
  via the structural renderer.
- `make lint` clean; snapshot tests for both extraction modes.

## Phase 2 — On-disk extraction cache

### Motivation (numbers from Phase 1 smoke)

End-to-end smoke on the HBR Entrepreneur's Handbook (286 pages):
- pymupdf4llm: ~150ms/page × 286 = ~40s
- + docling fallback firing on ~1 page: ~3s
- Total: **~43s for one 286-page book on reindex**

A typical fnd corpus is several hundred such books. Every
`fnd collection reindex` (today, with no caching) re-pays the full
extraction cost for *every* file, even files that haven't changed
since the last index build. For a ~200-book corpus: ~2.5 hours of
wasted CPU. **This is the primary blocker to making the extras-enabled
workflow practical.**

### Cache key (chosen: content-addressed)

The Phase 0 spec presented three options; Phase 0's measurements
settled the choice:
- median extraction is **multi-second per file** (not <50ms/page) →
  Option A (no cache) is out
- file mtimes drift under Dropbox/Syncthing/rsync, common in
  real fnd corpora → Option B (mtime+path) too lossy
- one-time sha256 of a 5MB PDF is ~10ms on M1 Max — negligible vs
  multi-second extraction → **Option C (content hash) wins**

Key composition:
```
cache_key = sha256(file_bytes) || extractor_id || extractor_version
where extractor_id ∈ {"flat", "pymupdf4llm", "docling-hybrid"}
      extractor_version is the package version + any config-shaping flags
```

Different extractor → different key → independent cache entries.
A user can install extras, build cache, uninstall extras, reinstall a
different combo, and never re-extract a file that's identical bytes-wise.

### Storage layout

```
$XDG_CACHE_HOME/fnd/extraction/
  <first-2-of-sha256>/
    <sha256>--<extractor_id>--<extractor_version>.json
```

Sharded by hash prefix to keep any single directory below filesystem
inode-list limits. Each artifact is a single JSON blob:
```json
{
  "schema_version": 1,
  "source_sha256": "...",
  "extractor_id": "docling-hybrid",
  "extractor_version": "pymupdf4llm-1.27.2.3+docling-2.94.0",
  "extracted_at": "2026-05-20T15:30:00Z",
  "source_size_bytes": 5242880,
  "chunks": [
    {"body": "...", "body_md": "...", "body_struct": [...], ...},
    ...
  ]
}
```

JSON over a binary format because:
- Trivial to inspect (`jq` works)
- No native binary deps needed
- A 300-page book's extraction is ~500KB JSON; loadable in <50ms
- Forwards-compat: `schema_version` field lets us migrate

### Lifecycle

**Read path** (extractor):
```python
def extract(path: Path) -> Iterator[Chunk]:
    cache = ExtractionCache.default()
    key = cache.key_for(path, extractor_id=_current_extractor_id())
    cached = cache.get(key)
    if cached is not None:
        yield from cached.chunks
        return
    chunks = list(_extract_uncached(path))
    cache.put(key, chunks, source_size=path.stat().st_size)
    yield from chunks
```

**Write path**: atomic (`os.replace` from a tmp file in the same
directory) to survive Ctrl+C mid-write.

**Invalidation**: never automatic on content change — that's what the
content hash *is* for. On version bump, the cache key changes
naturally so old entries become unreachable; we keep them for one
release cycle then prune on next `fnd cache prune`.

### CLI

```
fnd cache status                # show cache dir, total size, entry count
fnd cache prune                 # delete entries from old extractor versions
fnd cache clear [--yes]         # nuke the whole cache
fnd cache info <path>           # show which entry would be used for a file
```

`fnd extras uninstall pdf-structure` does NOT clear the cache —
existing entries stay, are unreachable for new extractor configs but
remain valid if the user reinstalls the same extras version.

### Memory pressure considerations

A reindex of N PDFs accumulates N×~500KB JSON files. A 1000-book
corpus = ~500MB cache. Document in `fnd cache status`; let the user
prune. No automatic eviction (LRU etc.) — fnd's corpora are bounded
and explicit pruning is simpler than tuning a policy.

### Phase 2 deliverables

1. `fnd/cache.py` — `ExtractionCache` class + `Chunk`<->JSON
   serialisation
2. `fnd/extract/pdf.py` — integrate cache lookup at top of `extract()`
3. `fnd/extract/__init__.py` — same for other extractors that benefit
   (eventual; PDF first)
4. `fnd/cli.py` — `cache_app` Typer subcommand (status/prune/clear/info)
5. Tests:
   - F11: identical file content → cache hit
   - F12: same file content, different extractor → cache miss
   - F13: cache write is atomic (Ctrl+C survives)
   - F14: `fnd cache prune` removes only old-extractor entries
   - F15: corrupt cache entry → silent miss + log + re-extract
   - NF8: cache lookup adds <20ms to extract() per file
   - NF9: cache size growth proportional to indexed corpus, not unbounded
6. README cache section
7. End-to-end verification: cold reindex of HBR handbook → warm reindex
   should drop from ~43s to <2s (cache hits)

### Cache-driven resumability (no separate machinery needed)

The cache is also the resumability primitive. Every file's extraction
completion writes a cache entry on disk; the indexer's per-file loop
already checks the cache at the top of `extract()`. So:

- Ctrl+C / terminal close / sleep mid-reindex → re-run
  `fnd collection reindex <name>` and only the files not yet cached
  get re-extracted.
- No "resume" flag needed; the cache lookup handles it.

One small optimisation to add in Phase 2: when the cache hits AND the
Tantivy index already has chunks for this `parent_id` at this mtime,
skip the delete+rewrite of those chunks too. Without this, warm
reindex is O(JSON-load) per file; with this, it's O(metadata-check)
per file. Difference: ~5ms vs ~50ms per cached file = matters on
big corpora.

### Out of scope for Phase 2

- LRU eviction (manual prune is enough)
- Cross-machine cache portability (sha256 makes this technically
  trivial but no UX yet)
- Per-page granularity (cache key is per-file; if extraction of page 50
  changes, we re-extract the whole file). Reasonable because:
  - pymupdf4llm processes whole-doc anyway
  - docling daemon does too
  - File-level change → file-level recompute is the natural unit

## Phase 2.5 — In-app progress UI + background runs

Even with caching, the first cold reindex of a real corpus can be
hours. A CLI-only flow (block terminal until done, no progress
feedback, no way to dismiss) is unworkable. fnd needs an in-app
indexer with a progress UI and the ability to run in the background
within the TUI's lifetime.

### Surface (TUI command palette)

```
> reindex default
```

opens a modal dialog:

```
┌─ Indexing: Documents/Readings ─────────────────────────────┐
│                                                            │
│  142 / 487 files                          [████░░░░░░] 29% │
│                                                            │
│  Current: (HBR Handbooks) coll. - The Harvard Business…    │
│  Page 187 / 286 · docling fallback fired on 3 pages        │
│                                                            │
│  Started: 14:23   Elapsed: 18 min   ETA: ~45 min remaining │
│  Cache: 38 hits, 104 misses                                │
│                                                            │
│  [ Pause ]  [ Run in background ]  [ Cancel ]              │
└────────────────────────────────────────────────────────────┘
```

### Behaviours

- **Run in background**: dismisses the dialog; indexer continues as an
  asyncio task owned by FNDApp. A thin one-line status indicator
  appears in the footer/status bar ("indexing 142/487 · click to
  view"). Clicking re-opens the modal.
- **Pause**: completes the current file, writes a checkpoint to the
  state file, stops. Re-opening the modal offers Resume.
- **Cancel**: stops at next file boundary. Cache entries written
  during the run remain (so a future `reindex` skips them).
- **Quit fnd mid-reindex**: the asyncio task dies; cache entries
  written so far survive. **On next launch fnd auto-resumes** —
  the indexer task starts in background mode immediately, and the
  user sees the "indexing 142/487 · click to view" footer indicator.
  No confirmation prompt: closing fnd shouldn't punish the user with
  a dialog on next launch, and the cache makes resume effectively
  free (cache hits skip the ~150ms extraction).

  If the user wants to stop a resumed reindex, they click the
  footer to open the modal and hit Cancel. To disable auto-resume
  entirely: `fnd config set indexer.auto_resume = false` (default
  true).

  If a reindex was *cancelled* (not just interrupted by quit), the
  state file is cleared and there's nothing to resume.

### Architecture

- `fnd/index_runner.py` — the async indexer task. Mirrors
  `build_index_from_config` but yields per-file progress events
  instead of blocking until done. Uses `asyncio.to_thread` for the
  per-file pymupdf4llm + docling work so the event loop stays
  responsive for UI rendering.
- `fnd/tui/indexer_modal.py` — Textual ModalScreen with the layout
  above. Reads events from a `asyncio.Queue` shared with the runner.
- `fnd/tui/app.py` — adds a command-palette entry, a footer status
  widget, and a startup hook that reads the state file.
- State file: `$XDG_DATA_HOME/fnd/reindex/<collection>.state.toml` —
  small TOML with `started_at`, `total_files`, `files_completed`,
  `current_file`, `interrupted_at`. Atomic-writes per-file completion.

### First-reindex disclosure (when extras flip the cost profile)

Installing the `pdf-structure` extra raises per-PDF indexing cost
from ~1-2s to ~30-60s on first extraction. The user already saw
disk-impact disclosure at `fnd extras install` time; they need an
**indexing-time** disclosure too, ideally right before the first
big reindex bills hours of CPU.

Trigger: the first `> reindex` palette command issued after a state
transition where `_HAS_PYMUPDF4LLM` flipped from False → True
(detected via a flag in fnd's data dir, set by the indexer after
its first run with extras). Shows:

```
┌─ First reindex after enabling structured PDF support ──────┐
│                                                            │
│  This will extract structure from 487 PDFs in 'default'.   │
│                                                            │
│  Estimated time: ~2h 30min (~30s per PDF on average,       │
│  ~3min on figure-heavy PDFs with image-table fallback).    │
│                                                            │
│  After this one-time cost, future reindexes only process   │
│  files that have changed since last run.                   │
│                                                            │
│  Indexing runs in the background — you can keep searching  │
│  while it works. fnd will auto-resume on next launch if    │
│  you quit before it finishes.                              │
│                                                            │
│  [ Start ]  [ Cancel ]  [ Don't show this again ]          │
└────────────────────────────────────────────────────────────┘
```

ETA computed from: `pdf_count × avg_pages × ~150ms` + a ~15% kicker
for docling-fallback pages. Re-estimated and updated in the
progress modal as the run progresses (replace static ETA with
"running average × remaining files").

The "Don't show this again" option persists in fnd config; can be
re-enabled via `fnd config set indexer.show_first_reindex_warning = true`.

The same disclosure also appears in the `fnd extras install
pdf-structure` confirmation flow, with the indexing-time numbers
appended after the existing disk-size disclosure:

```
This will install structured PDF rendering, which uses two
extractors:

  pymupdf4llm[layout]  (Polyform Noncommercial)  ~200 MB
  docling-slim         (Apache-2.0)              ~700 MB

Approximate total disk: ~900 MB

After installing, your next `fnd collection reindex` will spend
~30s per PDF extracting structure (one-time per file, cached
afterward). For a corpus of 100 books that's roughly 50 minutes;
500 books is roughly 4 hours. Subsequent reindexes only re-process
changed files.

Continue? [y/N]
```

### Phase 2.5 deliverables

1. `fnd/index_runner.py` — async indexer + progress events
2. `fnd/tui/indexer_modal.py` — progress dialog + first-reindex warning dialog
3. `fnd/tui/app.py` — palette command, footer status, auto-resume on launch
4. State file format + atomic-write helper
5. ETA estimator (initial estimate + running-average updates)
6. `fnd/extras.py` — extend install disclosure with indexing-time numbers
7. Tests:
   - F16: starting a reindex from palette opens the modal
   - F17: "Run in background" dismisses modal, indexer continues
   - F18: closing fnd mid-reindex writes a state file; reopening
     auto-resumes (no prompt) with footer indicator
   - F19: cancelling a reindex clears the state file so next launch
     doesn't auto-resume
   - F20: cache-hit files don't show up in the progress count
   - F21: first-reindex warning appears once then suppresses on
     "don't show again"
   - F22: `fnd config set indexer.auto_resume = false` suppresses
     auto-resume
   - NF11: UI stays responsive during indexing (frame rate >30fps in
     async-runner mode)
   - NF12: extras install disclosure includes indexing-time estimate

### Background-helper alternatives (deferred)

A launchd-managed daemon would let reindex survive fnd quit + reboot.
Honest cost:
- Apple Developer ID required for Homebrew distribution (already a
  concern fnd's SECURITY.md navigates for the main binary)
- Separate `fnd-indexd` binary with its own update channel
- IPC protocol (Unix socket or gRPC) between TUI and daemon
- Permissions UX (background-task entitlement on macOS)

Defer until users actually report wanting reindex to survive fnd
quit. The cache already makes a "quit + relaunch + resume" flow
fast enough that this is probably YAGNI.

## Phase 2.6 — Preview pane parity verification

Not really a new phase; a verification step that should happen as
part of Phase 2 acceptance. Goal: PDF preview-pane load with warm
cache is **indistinguishable** from MD file preview load.

The plumbing is already there from Phase 1 — adding `"pdf"` to
`_MARKDOWN_RENDERED_KINDS` routes PDFs through the same structural
pipeline as MD/DOCX. With Phase 2 caching:

- Cache hit at preview time = JSON load (~10-30ms for a 300-page
  book's chunk blob) + structural widget mount
- The existing prefetch mechanism (mount-on-tree-expand, cache flip
  on revisit) applies unchanged

### Verification (NF10)

- Open a known-cached PDF in the preview pane; time to first paint
  must be within 1.5× of a same-size MD file's first paint
- Snapshot test verifies the prefetch widget tree is built before the
  user clicks (mount-on-expand contract holds for PDFs)
- Manual: navigate through a 200-page book's match list with the
  arrow keys; preview re-mount latency must be sub-100ms on every
  step (the per-chunk prefetch + cache-flip already does this for MD;
  same path for PDF after Phase 1)

## Phase 3 — Per-page quality routing (hybrid pymupdf4llm + docling)

**Why two extractors:** pymupdf4llm is fast (~0.16s/page), preserves
inline formatting (bold/italic), and handles vector-line tables. It
fails on image-rendered tables — emitting a literal
`==> picture [W x H] intentionally omitted <==` marker where the table
should be. For a search tool, leaving table content un-indexed is
unacceptable. Docling's ML layout model catches those tables but
discards inline formatting and is ~3× slower per page.

Hybrid: run pymupdf4llm on every page; fall through to docling only
for pages where pymupdf4llm visibly missed structured content. Expect
~10-20% of pages to invoke docling on a typical book corpus
(concentrated in HBR/finance/data-heavy PDFs).

### Detection signals — when to fall through to docling

Computed during the pymupdf4llm pass, no extra parsing cost:

1. **Picture-omitted marker.** Regex `==> picture \[(\d+) x (\d+)\] intentionally omitted <==`
   on the markdown output. Each match gives the W×H of an un-decoded
   region.
2. **Region-size ratio.** Sum of omitted W×H divided by `page.rect.width *
   page.rect.height`. Trigger threshold: **>15%** of page area —
   filters out small logos, decorative figures, and headshots.
3. **Table-label proximity.** Look for `TABLE`, `Table`, `Fig\.?`,
   `Figure` in the page's text within ~5 lines of the omitted region.
   Strengthens the signal that the picture *is* a table.
4. **Text density.** Pages with rendered content area >50% but
   extracted text token count <50 → very likely scanned/image-heavy.
   Same fallback applies. (Out of scope for the OCR-disabled flow per
   spec non-goals, but docling can produce structure from the
   layout pass without OCR.)

```python
def needs_docling_fallback(page, pymupdf_md: str) -> bool:
    """Cheap heuristic — returns True if pymupdf4llm visibly missed content."""
    page_area = page.rect.width * page.rect.height
    omitted_area = sum(
        int(w) * int(h)
        for w, h in _PIC_RE.findall(pymupdf_md)
    )
    if page_area > 0 and omitted_area / page_area > 0.15:
        return True
    # cheap secondary: very low text density on a clearly-non-empty page
    if len(pymupdf_md.split()) < 50 and page.get_pixmap(dpi=36).is_unicolor is False:
        return True
    return False
```

### Routing flow

```
for page in doc:
    md, blocks, body = pymupdf4llm.extract_one(page)
    if needs_docling_fallback(page, md):
        try:
            md2, blocks2, body2 = docling.extract_one(page)
            md, blocks, body = md2, blocks2, body2   # full replacement
        except Exception as e:
            log.warning(f"docling fallback failed: {e}, keeping pymupdf4llm output")
    chunks.append(Chunk(body=body, body_md=md, body_struct=blocks, ...))
```

**Replacement vs splice.** First implementation: full replacement
(docling output wholesale supersedes pymupdf4llm for that page).
Loses pymupdf4llm's formatting on those pages but gets the table.
Splicing (keep pymupdf prose, swap in docling table at marker site)
is theoretically better but requires region-aligned merging — defer
to Phase 4 if the formatting loss on ~10% of pages bothers users in
practice.

### Docling lifecycle in the indexer

Docling daemon model (proven in the bake-off):
- Lazy-spawn on first need within a reindex run (one model load ~3s)
- Reuse across all pages of all PDFs that need it during this run
- Tear down at end of reindex / on Ctrl+C
- Daemon lives in docling-slim's own tool venv via subprocess (sidesteps
  the typer/pillow version conflicts with fnd's project deps)

If docling isn't installed, the fallback fails gracefully: keep
pymupdf4llm output with picture-omitted markers, emit one warning
per reindex run telling the user how to install. fnd remains
functional without docling, just with fewer tables indexed.

### Debug visibility

Add `fnd debug pdf <path> --route` that prints per-page routing
decisions:
```
page  1: pymupdf4llm    text=2143 chars
page  2: pymupdf4llm    text=1856 chars
page 98: DOCLING        reason=omitted-area-ratio=0.42 nearby-label="TABLE 5-2"
page 99: pymupdf4llm    text=1721 chars
```

### Open questions for Phase 3

- **Threshold tuning.** The 15% area threshold and 50-token density
  cutoff are guesses; needs spot-checking on the user's actual corpus.
- **False positives.** Large illustrative figures (photos in
  cookbooks, diagrams in textbooks) will trigger docling unnecessarily.
  Worst case: 0.4s wasted per page. Cache makes this one-time cost.
- **Phase 2 cache key.** Must include both `pymupdf4llm_version` and
  `docling_version` because reindex can change either extractor's
  output. Composite key: `sha256(file) + ("pymupdf4llm", v1) + ("docling", v2)`.

## Phase 4 — Ship *(sketch)*

Documentation, CHANGELOG entry, snapshot tests for the structural
preview path on PDFs, notes on reindex behaviour in the README.
Decision on whether to enable by default for new collections or
require explicit opt-in via `fnd config set`. Splice-merge
investigation (Phase 3 deferred work) if formatting loss on
docling-fallback pages turns out to matter.

## Phase 4 — Ship *(sketch)*

Documentation, CHANGELOG entry, snapshot tests for the structural
preview path on PDFs, notes on reindex behaviour in the README.
Decision on whether to enable by default for new collections or
require explicit opt-in via `fnd config set`.
