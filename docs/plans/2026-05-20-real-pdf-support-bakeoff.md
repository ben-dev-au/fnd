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

## Phase 3 — Per-page quality routing *(sketch)*

Implement the cheap-signal classifier (TOC presence, distinct font
sizes, column count, table count, scanned-page detection) and route
each page to the cheap path / rich path / OCR path. Plumb the
routing decision through to the chunk so we can debug why a given
page got its structure (or didn't). Add `fnd debug pdf <path>`
subcommand to dump per-page routing decisions.

## Phase 4 — Ship *(sketch)*

Documentation, CHANGELOG entry, snapshot tests for the structural
preview path on PDFs, notes on reindex behaviour in the README.
Decision on whether to enable by default for new collections or
require explicit opt-in via `fnd config set`.
