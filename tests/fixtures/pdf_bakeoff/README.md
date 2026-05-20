# PDF Bake-off Fixtures

The bake-off harness in `tools/pdf_bakeoff/` accepts any directory of
PDFs. The repo does **not** check in a fixture corpus — most PDFs we'd
want to test against are not ours to redistribute.

In practice, run the harness against:

1. The existing `tests/fixtures/papers/test.pdf` (CI smoke).
2. Your own document library — `~/Documents/Readings/`,
   `~/Downloads/`, any active `fnd` collection root.
3. Optionally, a small permissively-licensed set fetched on demand
   via `fetch_fixtures.sh`.

## Stratification — what we care about

A single-column memo and a multi-column physics paper are very
different problems. To form a defensible recommendation, sample across
these categories. Five PDFs per category is enough to form an opinion;
ten per category is enough to commit.

| Category | What it stresses | How to find them |
|---|---|---|
| Single-column born-digital | Sanity check — most extractors should ace this | Business memos, blog post exports, HBR-style articles |
| Multi-column scientific papers | Reading-order under columns — pymupdf4llm's known weak spot | arXiv preprints, conference proceedings, ACS journals |
| Table-heavy | Table boundary detection, in-table newlines | Financial reports, datasheets, government statistics |
| Slides exported to PDF | Landscape pages, sparse text, large fonts — exercises the slide-shape gate | Conference talks, lecture slides |
| Weird | Forms with fillable fields, mixed-language, decorative fonts, dense footnotes | Tax forms (IRS, HMRC), bilingual papers |

OCR-only PDFs (scanned without a text layer) are **out of scope** — the
real-PDF-support feature indexes the text layer only. See the spec
non-goals. The bake-off runners all disable OCR by default.

## Legal note

**Do not commit large PDFs to this repo.** The `test.pdf` already in
`tests/fixtures/papers/` is the only checked-in fixture. Anything else
gets fetched at runtime or pointed at via an absolute path.

`fetch_fixtures.sh` downloads a small set of permissively-licensed
PDFs (arXiv preprints under CC, IRS public forms, Project Gutenberg
ebook PDFs) into `~/Library/Caches/fnd/bakeoff/fixtures/` — outside
the repo, so a `git clean` won't blow them away.

## Running

```sh
# CI smoke — uses the checked-in test.pdf
make test

# Real corpus run — your own Readings folder
uv run python -m tools.pdf_bakeoff \
    ~/Documents/Readings /tmp/bakeoff-$(date +%Y%m%d) \
    --runners baseline,pymupdf4llm_layout,pymupdf4llm_legacy \
    --pages-per-pdf 5 --max-pdfs 30 --seed 42
```
