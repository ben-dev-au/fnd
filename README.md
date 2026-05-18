# FND

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

## Search how-to

fnd's query bar accepts plain words, phrases, boolean expressions, fuzzy and
proximity matches, field qualifiers, date filters, and markdown frontmatter
filters. They compose freely.

### The basics

| You type                      | What it does                                                                                                                                                           |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `entropy`                     | Single term. Matches anywhere in the document body, title, heading path, or filename. Stemmed, so `entropies` and `entropy` are equivalent.                            |
| `cross entropy loss`          | Three terms, implicit AND. Every term must appear somewhere in the chunk — but not necessarily near each other or in order.                                            |
| `"cross entropy loss"`        | Exact phrase. The three words must appear in order, adjacent. Matches `cross entropy loss` and `cross-entropy loss` (hyphens are treated as separators at index time). |
| `cross OR entropy`            | Either term. Useful when a concept goes by different names.                                                                                                            |
| `NOT regression`              | Exclude. Almost always combined: `entropy NOT regression`.                                                                                                             |
| `(loss OR cost) AND function` | Parentheses group boolean clauses.                                                                                                                                     |

### Phrase search vs loose AND

Quotes are the single biggest precision win:

- `man in the middle` — every doc with the words `man`, `in`, `the`, and
  `middle` _anywhere_ in a chunk. Lots of noise.
- `"man in the middle"` — only docs where those four words appear together,
  in order. Also matches `man-in-the-middle` (hyphens split into the same
  tokens at index time).

If you find yourself searching for a common phrase, quote it.

### Proximity — "near each other, not necessarily adjacent"

When you want the terms close together but don't care about exact order or
adjacent words between them, use a proximity (slop) search. Two equivalent
forms:

| You type                           | Means                                        |
| ---------------------------------- | -------------------------------------------- |
| `{5} cross entropy`                | The two terms within 5 tokens of each other. |
| `cross NEAR/5 entropy`             | Same.                                        |
| `{20} man in the middle attack`    | All five words within ~one line of text.     |
| `{60} buffer overflow exploit`     | Within ~a few lines.                         |
| `{500} race condition mitigations` | Within ~one page.                            |

Rough mapping: ~5 tokens = very near, ~20 = one line, ~60 = a few lines,
~500 = roughly a page. Proximity is bounded by chunk size — if the terms
straddle a chunk boundary, no proximity query will catch them; that's when
you fall back to loose AND.

### Fuzzy matching for typos and variants

Suffix `~1` or `~2` to allow that many edits per term:

| You type         | Matches                                            |
| ---------------- | -------------------------------------------------- |
| `mitochondira~1` | `mitochondria`, `mitochondrial`, etc.              |
| `kubernates~2`   | `kubernetes`, `kubernates`, `kubernetes` variants. |

Use sparingly on short terms — `cat~2` matches almost everything.

### Field qualifiers

Restrict matches to a specific field:

| You type                   | What it does                                                  |
| -------------------------- | ------------------------------------------------------------- |
| `title:transformer`        | Match only documents whose title contains `transformer`.      |
| `heading_path:"chapter 4"` | Match the section heading path.                               |
| `author:dijkstra`          | Match the document author metadata.                           |
| `kind:pdf`                 | Restrict to a file type (`pdf`, `docx`, `pptx`, `md`, `txt`). |
| `path_tokens:thesis`       | Match the filesystem path.                                    |

Combine with normal terms: `kind:pdf "diffusion model"` returns PDFs containing
the exact phrase.

### Collections

fnd organises sources into named collections. The shorthand `c:` scopes a
search to one or more:

| You type                     | What it does                       |
| ---------------------------- | ---------------------------------- |
| `c:wine attack`              | Search the `wine` collection only. |
| `c:notes,papers transformer` | Search two collections.            |

Without `c:` the active collection (settings menu) is used.

### Page, slide, and date filters

Numeric ranges use `[low TO high]`. Shorthand for one-sided comparisons:

| You type                                    | What it does                       |
| ------------------------------------------- | ---------------------------------- |
| `page:5`                                    | Exact page 5.                      |
| `page:>20`                                  | Page 21 onward.                    |
| `page:[10 TO 20]`                           | Pages 10–20 inclusive.             |
| `slide:<5`                                  | First four slides.                 |
| `mtime:today`                               | Modified today.                    |
| `mtime:week` / `mtime:month` / `mtime:year` | Within the last 7 / 30 / 365 days. |
| `mtime:>2024-01-01`                         | Modified on or after 2024-01-01.   |
| `mtime:[2024-01-01 TO 2024-06-30]`          | Modified in that ISO range.        |

### Wildcards

`*` matches zero or more characters at the end of a term:

| You type  | Matches                                                       |
| --------- | ------------------------------------------------------------- |
| `crypto*` | `crypto`, `cryptography`, `cryptographic`.                    |
| `*tion`   | Wildcard prefixes are not supported — anchor at the end only. |

### Markdown frontmatter filter

If you're searching across markdown notes with YAML frontmatter, append a
bracketed predicate that's evaluated against each note's frontmatter:

| You type                                    | What it does                                      |
| ------------------------------------------- | ------------------------------------------------- |
| `mitm [Course == "Security Foundations"]`   | Notes where the `Course` field equals that value. |
| `[Notes_Type in ["Lecture", "Tutorial"]]`   | All notes tagged Lecture or Tutorial.             |
| `entropy [Course == "ML" AND Year >= 2024]` | Compound predicate.                               |
| `[Tags ~~ "draft*"]`                        | Glob-match against the `Tags` field.              |

Supported operators: `==` `!=` `<` `<=` `>` `>=` `~~` (glob), `in`, `not in`,
`AND`, `OR`, `NOT`, parentheses. Values can be strings (quoted), numbers, ISO
dates, `true`/`false`/`null`. The filter applies only to markdown files; other
kinds pass through unfiltered.

### Composing — worked examples

```text
"buffer overflow"                                  # exact phrase
{10} buffer overflow exploit kind:pdf              # three terms within 10 tokens, PDFs only
c:notes mitm [Course == "Security Foundations"]    # term + collection scope + frontmatter filter
title:"chapter 4" heading_path:proof               # constrain to one chapter's proofs
kind:pptx slide:>10 attention                      # later-half slides mentioning attention
mtime:month NOT draft~1                            # recent docs, exclude anything close to "draft"
```

### A few common pitfalls

- **Quoting a single word does nothing useful.** `"entropy"` is the same as
  `entropy`. Quotes only help for multi-word phrases.
- **`OR` and `AND` are case-sensitive.** Lowercase `or` / `and` are treated
  as ordinary terms. Always uppercase boolean operators.
- **Stopwords aren't filtered.** `the man` matches docs containing both `the`
  and `man`. For common-word phrases, quote them or use proximity.
- **Proximity is per-chunk.** A phrase or `{N}` query can't span a chunk
  boundary. If the terms are paragraphs apart, drop to loose AND.
- **Wildcards on very short stems are slow.** `a*` will scan every term in
  the index. Use at least three letters before `*`.

## Acknowledgments

Some design choices in fnd's search layer are adapted from sibling
open-source projects:

- **[tobi/qmd](https://github.com/tobi/qmd)** (MIT): the strong-signal bypass
  (skip parallel sub-queries when the literal probe is already unambiguous),
  the score normalization `s / (1 + s)` that makes its thresholds (0.85
  score, 0.15 gap) corpus-stable, and the `intent:` line in the multi-line
  query DSL.
- The Reciprocal Rank Fusion constant `k = 60` and rank-position bonuses
  follow Cormack/Clarke/Buettcher (2009).
