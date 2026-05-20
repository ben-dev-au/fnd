# PDF Bake-off — Results

Generated: {{GENERATED_AT}}
PDF directory: `{{PDF_DIR}}`
PDFs processed: {{N_PDFS}}
Pages sampled: {{N_PAGES}}
Pages per PDF: {{PAGES_PER_PDF}} (seed={{SEED}})
Runners: {{RUNNERS}}

## Per-runner aggregates

{{RUNNER_TABLE}}

Notes:
- `mean_jaccard` is mean token-overlap with the baseline plain-text extraction.
  Higher = less content loss/duplication.
- `median_wall_ms` and `p95_wall_ms` are per-page extraction time, averaged
  across PDFs (so a 100-page PDF and a 5-page PDF contribute equally).
- See `metrics.csv` for raw per-page rows and `by_pdf/<stem>/<page>/<runner>.md`
  for side-by-side outputs.

## Hand-score table (fill in)

For 5-10 PDFs across strata, open the side-by-side Markdown files and score:

| pdf | stratum | best runner | reading order | tables | headings | comments |
|---|---|---|---|---|---|---|
|   |   |   |   |   |   |   |
|   |   |   |   |   |   |   |
|   |   |   |   |   |   |   |

Score scale: ✓ acceptable / ◐ partial / ✗ broken.

## Recommendation (fill in)

After running on a representative corpus slice, write 3-5 bullets:

- Winning extractor:
- Runner-up and why not winner:
- AGPL exposure status:
- Per-page latency observed (informs cache design A/B/C in the spec):
- Open follow-ups for Phase 1:
