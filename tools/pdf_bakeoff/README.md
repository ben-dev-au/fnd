# tools/pdf_bakeoff — Real PDF Support, Phase 0

Empirically compare PDF→Markdown extractors on a real corpus before
picking one for `fnd`. See `docs/specs/2026-05-20-real-pdf-support.md`
and `docs/plans/2026-05-20-real-pdf-support-bakeoff.md`.

## Usage

```sh
# CI smoke (single checked-in fixture; only baseline)
uv run python -m tools.pdf_bakeoff \
    tests/fixtures/papers /tmp/bakeoff-smoke \
    --runners baseline

# Default bake-off — built-in runners, no extra deps
uv run python -m tools.pdf_bakeoff \
    ~/Documents/Readings /tmp/bakeoff-readings \
    --pages-per-pdf 5 --max-pdfs 20 --seed 42

# All runners (requires extra installs)
pip install docling marker-pdf
uv pip install -U "mineru[all]"
uv run python -m tools.pdf_bakeoff \
    ~/Documents/Readings /tmp/bakeoff-full \
    --with-docling --with-marker --with-mineru \
    --pages-per-pdf 5 --max-pdfs 20
```

## Runners

| name | code license | weights license | extras |
|---|---|---|---|
| `baseline` | AGPL/commercial (pymupdf) | — | — |
| `pymupdf4llm_layout` | AGPL/commercial | — | — |
| `pymupdf4llm_legacy` | AGPL/commercial | — | — |
| `docling` | Apache-2.0 | Apache-2.0 | `pip install docling` |
| `marker` | GPL-3.0 | Modified Open Rail-M | `pip install marker-pdf` |
| `mineru` | MinerU OSL (Apache-2.0-based) | Custom | `uv pip install -U "mineru[all]"` |

Opt-in runners are lazy-imported and fail with an install hint when
missing. Their model weights download into `~/Library/Caches/fnd/bakeoff/<name>/`
on first run.

## Output layout

```
<out_dir>/
  metrics.csv         # one row per (pdf, page, runner)
  summary.csv         # one row per (pdf, runner)
  RESULTS.md          # aggregates + blank hand-score section
  by_pdf/
    <pdf_stem>/
      <page>/
        baseline.md
        pymupdf4llm_layout.md
        pymupdf4llm_legacy.md
        docling.md       # if --with-docling
        marker.md        # if --with-marker
        mineru.md        # if --with-mineru
```

## Metrics

- `wall_ms`, `rss_delta_mb` — per-page extraction cost.
- `n_h1`..`n_h6`, `n_tables`, `n_list_items` — structure recovered.
- `token_jaccard` — overlap with baseline `page.get_text("text")`. Pages
  below 0.7 are worth a human look for content loss/duplication.
- `reading_order_hash` — sha1 of normalized output; identical hashes
  across runs are a determinism check.

## Scoring rubric

Numbers tell us *cost* and *content fidelity*. Structure quality needs
human eyes — open the per-page side-by-side Markdown and score in
`RESULTS.md`. Five PDFs across strata is enough to form an opinion;
twenty is enough to commit.
