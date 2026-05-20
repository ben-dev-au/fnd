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

## Phase 1 — Integrate winning extractor *(sketch)*

After Phase 0 picks a winner, add it as a configurable extractor
behind a feature flag (`fnd config set pdf.extractor=...` or env
var). Populate `body_md` and a richer `body_struct` in
`fnd/extract/pdf.py`. Add `"pdf"` to `_MARKDOWN_RENDERED_KINDS` in
`fnd/tui/preview_dispatcher.py`. Keep the current font-clustering
path as fallback when the new extractor returns empty/garbage or
raises.

## Phase 2 — On-disk cache *(sketch)*

Implement the cache design selected in Phase 0 (see spec). If
content-addressed: schema-versioned artifact store under
`platformdirs.user_cache_dir("fnd") / "pdf_artifacts"`, keyed by
`sha256(file_bytes) + extractor_name + extractor_version`. Cache
lookup happens in the indexer before extraction is invoked.
Invalidation on extractor version bump. Add a `fnd cache clear` CLI
command. Wire the existing schema-bump migration prompt to also
offer cache reset.

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
