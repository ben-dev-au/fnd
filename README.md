# acorn

Fast, free, keyboard-driven document search for macOS. Indexes PDF, DOCX, PPTX, MD, and TXT
across multiple named collections, with strong BM25 ranking, in-file navigation, and a
lazygit-style TUI.

## Status

Early development. See `docs/specs/` and `docs/plans/` for the design spec and phase plans.

## Quick start (dev)

```sh
make sync          # uv sync --all-extras --group dev
make install-hooks # pre-commit hooks
make test          # run tests
make lint          # ruff + pyright strict
```

## Phases

- **Phase 1 (current)**: project skeleton, schema, TXT/MD/PDF extractors, `acorn index` and
  `acorn search` text CLI
- **Phase 2**: PPTX + DOCX with structure
- ...
- **Phase 5**: TUI shell
- ...

## Why this exists

Foxtrot Pro works but costs ~$200 AUD/licence and has crash issues on a 50k+ corpus.
Recoll's UX is clunky. PDF Search is PDF-only. acorn aims for Foxtrot-grade ranking and
in-file navigation, free, with a keyboard-only flow no commercial tool offers.

## Acknowledgments

Some design choices in acorn's search layer are adapted from sibling
open-source projects:

- **[tobi/qmd](https://github.com/tobi/qmd)** (MIT): the strong-signal bypass
  (skip parallel sub-queries when the literal probe is already unambiguous),
  the score normalization `s / (1 + s)` that makes its thresholds (0.85
  score, 0.15 gap) corpus-stable, and the `intent:` line in the multi-line
  query DSL.
- The Reciprocal Rank Fusion constant `k = 60` and rank-position bonuses
  follow Cormack/Clarke/Buettcher (2009).
