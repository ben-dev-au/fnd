# Malformed-input corpus

Drop minimised seed files here when Hypothesis or oss-fuzz finds an
extractor input that leaks something other than `ExtractError`. The
parametrised regression test in `tests/fuzz/test_extractor_fuzz.py`
re-runs every file against `fnd.extract.extract` on every fuzz pass —
so once a bug is fixed, the seed pins the fix.

Format: just drop the file in. Name it descriptively, e.g.
`pymupdf-cve-2024-xxxx.pdf` or `oss-fuzz-12345.docx`. No metadata file
needed.

Provenance hygiene: only commit files we own redistribution rights to
(oss-fuzz public corpora, our own minimisations, fixed-upstream
crashers under the upstream license).
