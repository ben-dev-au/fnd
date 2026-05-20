# Real PDF Support — Design Spec

**Status:** Draft. Phase 0 (bake-off) in progress on `feat/real-pdf-support`.
Phase 1+ blocked on bake-off results.

## Goal

PDF previews in `fnd` should look like the document does in Preview/Skim:
real headings, lists, tables, and reading-order-correct multi-column flow.
Today every PDF page becomes one big plain-text blob. That's good enough
for indexing — full-text search still finds the words — but it makes the
preview pane unreadable for anything more structured than a single-column
memo.

## Non-goals

- **Inline page rasterization** (Tdf / fancy-cat style). Adds binary
  blits to the TUI hot path and a hard dependency on a rendering backend.
  Deferred until structural rendering has been shipped and proven.
- **Figure-as-image extraction.** Useful for LLM-readiness, irrelevant
  for keyboard-driven preview. Deferred.
- **Equation rendering.** TeX/MathML in a terminal is its own project.
  Deferred.
- **Form-field interaction.** Out of scope — `fnd` is read-only over a
  corpus, not a PDF editor.

## Current behaviour

- `fnd/extract/pdf.py:225` — `extract(path)` yields one `Chunk` per
  non-empty page. Heading path comes from TOC (`_toc_heading_for_page`)
  with a font-clustering fallback (`_font_clustering_heading`, gated by
  `_is_slide_shape` and the "≤1 distinct font size" / ">30% spans
  flagged" bail-outs at `fnd/extract/pdf.py:178-222`).
- `fnd/extract/base.py:58` — `body_md` is documented as empty for PDF.
  `body_struct` carries a `Block(kind="h2", text=page_title)` followed
  by a single `Block(kind="p", text=text.strip())`.
- `fnd/tui/preview_dispatcher.py:29` — `_MARKDOWN_RENDERED_KINDS =
  frozenset({"md", "docx", "pptx"})`. PDF is absent → router always
  picks the flat path at `:52-55`, which routes through
  `LineBufferPreview` (one ScrollView, plain-text per line).
- Result for a research paper: one giant paragraph per page, no
  visible structure, no in-line code, no tables. Search still works.

## Desired behaviour

Per-page Markdown surfaced through the existing structural-renderer
path. The seam is the `Chunk` dataclass at `fnd/extract/base.py:36-79`:

- Populate `body_md` (the routing signal) so the dispatcher flips PDF
  to structural.
- Populate `body_struct` (a richer `list[Block]`) so the renderer at
  `fnd/render.py:187` produces real headings (`h1`-`h6`), `ul`/`ol`,
  `code`, `quote`. All the existing block kinds are already supported.
- Add `"pdf"` to `_MARKDOWN_RENDERED_KINDS`.

**Preserve the existing sanity-gate philosophy.** Bad structure is
worse than no structure. If the new extractor returns garbage
(determined by: empty output, jaccard against `page.get_text("text")`
below threshold, or a thrown exception), fall back to today's
plain-text path. Never replace plain text with worse-than-plain text.

## Architecture

**Per-page quality routing.** Cheap signals decide which extractor
runs for each page:

- TOC presence (`doc.get_toc()` non-empty for this page range)
- Distinct font sizes (already computed in `_font_clustering_heading`)
- Column count via `pymupdf4llm.helpers.multi_column.column_boxes(page)`
- Table-region count via `page.find_tables()`
- Scanned-page heuristic (text length ≈ 0 but image content present)

Cheap signals route to one of:

- **(a) Current fast plain-text path** — for pages where structure is
  uninteresting (single-column body text, slides with sparse content,
  documents without TOC and uniform font size).
- **(b) Richer Markdown-emitting extractor** — for pages with detected
  multi-column layout, tables, or rich heading hierarchy.
- **(c) Selective OCR re-run** — for scanned pages without an OCR
  layer (requires the existing `[ocr]` extra).

**Caching seam.** Per-page extraction is cached so the preview hot
path never re-parses PDFs and reindexes don't re-pay extraction cost.
See "Caching — decisions to make" below.

## Caching — decisions to make

Extraction with a heavy ML extractor (Docling, Marker, MinerU) can run
to seconds per page. A 500-page book reindexed when only its mtime
changed shouldn't re-extract every page. Two collections that share a
file shouldn't extract it twice.

Three candidate designs. The Phase 0 bake-off measures per-page
extraction wall-time and output stability to inform the choice:

### A. No cache
Every reindex re-extracts. Simplest possible code path. Acceptable
only if the chosen extractor is fast enough that re-doing 500 pages
is a sub-second cost. The bake-off `summary.csv` answers this
directly.

### B. (path, mtime, size) cache
Cheap key, no hashing required. Persisted alongside the Tantivy
index. Pros: no I/O beyond `stat()`. Cons: mtime drifts under
syncthing/Dropbox/rsync — false invalidations. Doesn't dedupe
across collections that include the same file at different absolute
paths.

### C. Content-addressed cache
Key = `sha256(file_bytes) + extractor_name + extractor_version`.
Cache lives in `platformdirs.user_cache_dir("fnd") / "pdf_artifacts"`
— deliberately *outside* the Tantivy index so it can be cleared
independently and shared across collections. Pros: robust to path/
mtime drift; same file in two collections extracts once; extractor
version bump invalidates cleanly. Cons: one-time sha256 read per
file (cheap — ~500MB/s on Apple Silicon, dominated by disk).

**Open Question: pick A, B, or C after Phase 0.** The bake-off feeds
the decision:

- If median extraction is <50ms/page → A is fine.
- If extraction is 100-500ms/page → B is enough.
- If extraction is multi-second/page (Docling-class) → C is
  necessary; the sha256 cost is rounding error against extraction.

## Tradeoffs and risks

- **Extractor regressions across versions.** `pymupdf4llm 1.27 →
  1.28` could shift output without warning. Cache invalidation must
  key on extractor version. Pin major in `pyproject.toml`.
- **Table width overflow in the Textual Markdown widget.** Wide
  tables wrap awkwardly. Mitigation: render tables as collapsible
  block in Phase 4 if it's bad in practice.
- **Multi-column reading-order failures.** pymupdf4llm has known
  issues — see [langchain#30931](https://github.com/langchain-ai/langchain/issues/30931),
  [pymupdf4llm#78](https://github.com/pymupdf/pymupdf4llm/issues/78).
  The bake-off must include multi-column papers as a stratum and
  measure jaccard + manual reading-order spot-check.
- **Licensing matrix.** First-class concern, not a footnote. The
  bake-off measures all candidates; this table documents the legal
  posture so the choice can weigh quality against redistribution risk.

  | Extractor | Code license | Model/weights license | Verdict for redistribution |
  |---|---|---|---|
  | PyMuPDF baseline | AGPL-3.0 / commercial | — (no models) | Existing dep; existing posture |
  | pymupdf4llm | AGPL-3.0 / commercial | — | Existing dep; AGPL is a blocker for commercial redistribution. Track. |
  | Docling (IBM) | Apache-2.0 | Apache-2.0 | Clean. Preferred if quality is competitive. |
  | Marker (datalab-to) | GPL-3.0 | Modified Open Rail-M (free for research / personal / orgs <$2M revenue) | OK at current scale; flag for growth. |
  | MinerU (opendatalab) | MinerU Open Source License (custom, Apache-2.0-based; moved off AGPL in 3.1.0) | Custom | Read LICENSE during integration to confirm. |

- **Model weight size.** Docling ~200-400MB. Marker ~5GB total.
  MinerU varies. First-run downloads must show progress and land
  in `user_cache_dir`, not a hidden temp dir.
- **Index-time latency.** Per-page extraction in the seconds-class
  is acceptable because cached. Without a cache, multi-second
  extractors are unusable on books. See "Caching".

## Open questions for the bake-off to answer

Specific, falsifiable:

1. Does `pymupdf4llm` `layout=True` reliably beat `legacy` on
   multi-column PDFs in our corpus, and by how much (jaccard, table
   count, hand-score)?
2. Is Docling worth +X seconds/page on our corpus? Quantify X from
   the data.
3. Do Marker / MinerU deliver materially better structure than
   Docling on table-heavy or scanned-with-OCR PDFs, and is the
   licensing cost worth it?
4. What fraction of pages in the user's corpus would actually
   benefit from the richer extractor (need quality routing), vs.
   the cheap path being good enough?
5. What's the per-page extraction wall-time distribution that the
   cache design needs to amortize? (Answers Caching A/B/C.)
6. Does AGPL exposure (pymupdf4llm) force the choice toward Docling
   even if Docling is slightly worse?
