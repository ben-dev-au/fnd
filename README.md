# FND

[![CI](https://github.com/ben-dev-au/fnd/actions/workflows/ci.yml/badge.svg)](https://github.com/ben-dev-au/fnd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](#requirements)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/ben.dev.au)

Fast, free, keyboard-driven document search for macOS. Indexes PDF, DOCX, PPTX,
MD, and TXT across multiple named collections, with strong BM25 ranking, in-file
navigation, an "Open with…" launcher, and a lazygit-style TUI.

> **macOS only, for now.** fnd relies on macOS file APIs and the `open` URL
> handler. Linux/Windows aren't supported yet.

## Status

Initial development all but complete and stable, core features implemented, entering a refinement period, finding, fixing and refining.

## Requirements

- macOS
- Python 3.13 (supplied automatically by Homebrew or pipx)
- [uv](https://docs.astral.sh/uv/), only for the optional structured-PDF extra

## Install

```sh
brew install ben-dev-au/tap/fnd
```

…or:

```sh
pipx install fnd
```

To independently verify the install:

```sh
gh attestation verify "$(brew --cache fnd)" --repo ben-dev-au/fnd
```

See [`SECURITY.md`](SECURITY.md) for the threat model, disclosure policy, and the
reasoning behind the install/verify story (no Apple Developer ID required;
Homebrew installs bypass Gatekeeper via curl).

## Features

- **Multi-format indexing**: PDF, DOCX, PPTX, Markdown, and plain text.
- **Named collections**: group sources (per-source roots, include/exclude
  globs, optional symlink-following) and search them individually or together.
- **Strong ranking**: BM25 with regime-aware fusion (strong-signal / fusion /
  cascade) for stable results across corpora of different sizes.
- **Expressive query language**: phrases, boolean, proximity, fuzzy, field
  qualifiers, wildcards, date filters, and markdown-frontmatter predicates
  (see [Search how-to](#search-how-to)).
- **lazygit-style TUI**: live search as you type, syntax-highlighted preview,
  and in-file navigation that jumps to the matching PDF page, PPTX slide, or
  Markdown heading.
- **"Open with…" launcher**: open a hit in Preview, Skim, Obsidian, VS Code,
  PDF Expert, or your own configured app, with page/line/heading deep-links
  where the app supports them (see [Open with…](#open-with-apps)).
- **Obsidian integration**: vault auto-detection, frontmatter filters, and
  line-precise jumps via the Advanced URI plugin.
- **Structured PDF extraction (opt-in)**: headings, lists, tables, and
  bold/italic, with a shared content-addressed extraction cache and
  auto-resume on interrupted reindexes.
- **Local and private**: no network, no telemetry. The index lives on your
  machine; state is hardened to `0o700`.

## Quick start

```sh
fnd index ~/Documents/papers      # ad-hoc index a folder into the default collection
fnd search "diffusion model"      # search from the terminal
fnd                               # launch the interactive TUI
```

For ongoing use, define collections (see [Collections & sources](#collections--sources))
and reindex them with `fnd collection reindex <name>`.

## Using the TUI

Run `fnd` with no arguments for the interactive interface. It has three panes:
the **query bar** at the top, the **results tree** (hits grouped by file) on the
left, and the **preview pane** on the right showing the matching passage with
your search terms highlighted. Just start typing, and results update as you go,
and the [query language](#search-how-to) works exactly as it does from the CLI.

### Moving around with the keyboard

| Key       | What it does                                                                           |
| --------- | -------------------------------------------------------------------------------------- |
| `↑` / `↓` | Move the cursor up/down through results (vim's `k` / `j` also work).                   |
| `→`       | Expand the focused file to its matching sections; press again to drill into the first. |
| `←`       | Collapse the focused node, or back out to its parent (lazygit-style).                  |
| `Tab`     | Cycle focus between the query bar, the results tree, and the preview.                  |
| `/`       | Jump back to the query bar to refine your search.                                      |
| `↑` / `↓` | When the preview pane is focused, scroll the preview.                                  |

### Opening and acting on a result

| Key            | What it does                                                                                                                                        |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `o`            | Open the hit in its resolved app, jumping to the matching page / slide / line / heading.                                                            |
| `O`            | **Open with…**: a picker of every app that handles this file type. Use `↑↓` then `Enter`, or press the letter shown next to an app; `Esc` cancels. |
| `Space`        | Quick Look the file.                                                                                                                                |
| `:`            | Open the **Settings & Commands** menu: every setting and action in one searchable, full-screen list.                                               |
| `?`            | Keybindings cheat sheet (press again to dismiss).                                                                                                   |
| `Ctrl+F`       | Toggle auto-fuzzy matching (persists to your config).                                                                                               |
| `h`            | Toggle search-term highlighting in the preview.                                                                                                     |
| `q` / `Ctrl+C` | Quit. `Esc` backs out of any overlay or nested screen.                                                                                              |

Inside the Settings menu (`:`) navigate with `↑↓` (or `j`/`k`), press `Enter` to
open / edit / toggle the focused row, `/` to filter rows by label, and `Esc` or
`←` to step back.

## Command reference

| Command                                         | What it does                                                                   |
| ----------------------------------------------- | ------------------------------------------------------------------------------ |
| `fnd`                                           | Launch the interactive TUI.                                                    |
| `fnd <query>`                                   | Launch the TUI with `<query>` pre-filled.                                      |
| `fnd -c <collection> <query>`                   | Launch the TUI scoped to a collection.                                         |
| `fnd tui [query]`                               | Explicitly launch the TUI (optional seed query).                               |
| `fnd search "<query>"`                          | Terminal search. Flags: `--limit`, `-c/--collection`, `--meta`, `--explain N`. |
| `fnd index <root>`                              | Ad-hoc index a single root into the default collection.                        |
| `fnd collection list`                           | List configured collections and their sources.                                 |
| `fnd collection add <name>`                     | Add (or extend) a collection in the config TOML.                               |
| `fnd collection reindex <name>`                 | Index or re-index a configured collection (`--rebuild` to start fresh).        |
| `fnd config show`                               | Print the effective merged config as JSON.                                     |
| `fnd config path`                               | Print the path to the config TOML.                                             |
| `fnd config edit`                               | Open the config TOML in `$EDITOR` (creates a template if missing).             |
| `fnd config validate`                           | Validate the config TOML.                                                      |
| `fnd extras list`                               | List optional extras and their installed status.                               |
| `fnd extras status`                             | Show installed extras with disk usage.                                         |
| `fnd extras install <name>`                     | Install an extra after a disk-impact disclosure prompt.                        |
| `fnd extras uninstall <name>`                   | Remove an extra (indexed chunks remain).                                       |
| `fnd cache status` / `info` / `prune` / `clear` | Manage the PDF extraction cache.                                               |
| `fnd version`                                   | Print the fnd version.                                                         |

## Open with… apps

In the TUI, `o` opens a hit in its resolved app and `O` opens the **Open with…**
picker. Built-in handlers ship for **Preview, Skim, Obsidian, VS Code, PDF
Expert, and System Default**; where the app and file type allow it, fnd jumps to
the matching page, slide, line, or heading. Set a per-file-type default with
`[app_defaults]`, or a per-source app, in your config.

You can add your own apps with a small `[apps.<id>]` block in your config; see
the catalogue and schema in [`docs/apps.md`](docs/apps.md). User templates are passed
to apps as argv lists (never a shell) or as percent-encoded URLs handed to
`open`, so file paths can't inject commands.

## Collections & sources

A **collection** is a named group of source folders you search together; each
**source** is a folder plus the include/exclude globs that decide which files in
it get indexed. The `default` collection points at `~/Documents` out of the box.
There are three ways to manage them, and they're interchangeable, because the UI
writes the same config file you can edit by hand.

### From the TUI

Press `:` to open Settings, move to **Collections**, then:

- **Add a collection**: choose **Add collection** and fill the wizard:
  **Name**, a **Source path** (a folder; `~/…` is fine), the file types to
  **Include** and patterns to **Exclude**, an optional markdown
  **Frontmatter filter**, and a **Follow symlinks** toggle. Press **Ctrl+S** to
  save and index right away (`Esc` cancels).
- **Add a source to an existing collection**: open the collection, then
  **Sources → Add source**, and set the path, includes/excludes, an optional
  per-source app, and (for Obsidian) the vault name. **Ctrl+S** saves and
  returns; **Ctrl+A** saves and adds another. Reindex the collection afterward.

### From the command line

```sh
# Create a collection with one source (repeat --source for more folders)
fnd collection add papers --source ~/Documents/Research

# Narrow it with globs, or add a markdown frontmatter filter
fnd collection add notes --source ~/Notes --include "**/*.md" --exclude "drafts/**"

fnd collection list             # show what's configured
fnd collection reindex papers   # build/update the index (--rebuild to start fresh)
```

### From the config file

Run `fnd config edit` to open the TOML in `$EDITOR` (the first run writes a
commented starter template), then `fnd config validate` to check it. UI edits
preserve your comments and formatting, so hand-editing and the Settings UI mix
freely.

## Configuration

The config lives at `~/Library/Application Support/fnd/config.toml` (run
`fnd config path` to confirm; fnd also reads `~/.config/fnd/config.toml` if you
keep it there). `fnd config show` prints the effective merged config;
`fnd config validate` checks it before you rely on it.

Each collection is one or more `[[collections.<name>.sources]]` tables. A
minimal, annotated config:

```toml
[defaults]
collection    = "papers"   # active collection when -c is omitted
result_limit  = 200        # max results per query
fuzzy_enabled = true       # auto-fuzzy in the cascade fallback (toggle with Ctrl+F)

# A collection named "papers" with two source folders.
[[collections.papers.sources]]
path     = "~/Documents/Research"
includes = ["**/*.pdf", "**/*.md"]        # omit to index all supported types
excludes = ["**/.git/**", "archive/**"]
follow_symlinks = false

[[collections.papers.sources]]
path               = "~/Notes"
includes           = ["**/*.md"]
frontmatter_filter = "Status == 'published'"   # markdown sources only; see Search how-to

# Default app per file type for the `o` shortcut.
# Built-in ids: system, preview, skim, pdf_expert, obsidian, vscode.
[app_defaults]
pdf = "skim"
md  = "obsidian"

# Define your own app (ready-made blocks live in docs/apps.md).
[apps.marked]
display_name = "Marked 2"
handles      = ["md"]
argv         = ["open", "-a", "Marked 2", "{path}"]
```

The `[defaults]` table also controls preview behaviour and auto-resume; run
`fnd config edit` to see every option documented inline. After changing
collections or sources, run `fnd collection reindex <name>` (or Reindex from the
Settings UI) to apply it.

## Indexing

### Structured PDF extraction (opt-in)

PDFs render as flat extracted text by default. The opt-in `pdf-structure` extra
adds headings, lists, tables, bold/italic, and recovered image-rendered tables.
It is installed via [uv](https://docs.astral.sh/uv/) (`brew install uv` if you
don't have it).

In the TUI: **Settings → Indexing → Status / Install…** shows current state,
disk impact (`~900 MB`), and a tight disclosure before any download. Install
runs in a modal with progress; **Esc** sends it to the background, **c** cancels
(SIGTERM).

From the CLI:

```sh
fnd extras install pdf-structure   # ~900 MB total, with disclosure prompt
fnd extras list                    # show available + installed
fnd extras status                  # disk usage per installed extra
fnd extras uninstall pdf-structure # revert; indexed chunks remain in index
```

After installing, reindex from **Settings → Collections → ‹name› → Reindex** (or
`fnd collection reindex <name>`). New PDFs added later are extracted structurally
automatically.

Two packages: `pymupdf4llm` (which pulls `pymupdf-layout`, Polyform
Noncommercial; fnd is non-commercial, acceptable) and `docling-slim[standard]`
(Apache-2.0). ML weights (~400 MB) download on first use. Uninstall removes the
packages; indexed structured chunks remain in the index until the next reindex.

### Cost on first reindex

~30 s per PDF on M1 Max (pymupdf4llm; longer for pages routed through the docling
fallback). **A 200-book corpus is roughly a 2-hour one-time cost.** Subsequent
reindexes only re-process changed files.

### Cache

Extracted chunks are content-addressed at `~/Library/Caches/fnd/extraction/`.
Shared across collections: the same file in two collections is extracted once.

In the TUI: **Settings → Indexing → Cache size** shows entries + disk;
**Cache maintenance…** drills to Prune stale (recoverable) and Clear
(destructive, confirms with `⚠ Cannot be undone`).

From the CLI: `fnd cache status / info / prune / clear`.

### Auto-resume on launch

A Ctrl+C, sleep, terminal close, or fnd quit during reindex leaves the cache and
a state file at
`~/Library/Application Support/fnd/reindex/<collection>.state.toml`.

Reopen the TUI and indexing auto-resumes silently in the background.
Already-cached files return in milliseconds, so resume effectively starts where
you left off.

Toggle off from **Settings → Indexing → Auto-resume on launch**, or set
`defaults.indexer_auto_resume = false` in your config.

## Search how-to

fnd's query bar accepts plain words, phrases, boolean expressions, fuzzy and
proximity matches, field qualifiers, date filters, and markdown frontmatter
filters. They compose freely.

### The basics

| You type                      | What it does                                                                                                                                                           |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `entropy`                     | Single term. Matches anywhere in the document body, title, heading path, or filename. Stemmed, so `entropies` and `entropy` are equivalent.                            |
| `cross entropy loss`          | Three terms, implicit AND. Every term must appear somewhere in the chunk, but not necessarily near each other or in order.                                            |
| `"cross entropy loss"`        | Exact phrase. The three words must appear in order, adjacent. Matches `cross entropy loss` and `cross-entropy loss` (hyphens are treated as separators at index time). |
| `cross OR entropy`            | Either term. Useful when a concept goes by different names.                                                                                                            |
| `NOT regression`              | Exclude. Almost always combined: `entropy NOT regression`.                                                                                                             |
| `(loss OR cost) AND function` | Parentheses group boolean clauses.                                                                                                                                     |

### Phrase search vs loose AND

Quotes are the single biggest precision win:

- `man in the middle`: every doc with the words `man`, `in`, `the`, and
  `middle` _anywhere_ in a chunk. Lots of noise.
- `"man in the middle"`: only docs where those four words appear together,
  in order. Also matches `man-in-the-middle` (hyphens split into the same
  tokens at index time).

If you find yourself searching for a common phrase, quote it.

### Proximity: "near each other, not necessarily adjacent"

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
~500 = roughly a page. Proximity is bounded by chunk size: if the terms
straddle a chunk boundary, no proximity query will catch them; that's when
you fall back to loose AND.

### Fuzzy matching for typos and variants

Suffix `~1` or `~2` to allow that many edits per term:

| You type         | Matches                                            |
| ---------------- | -------------------------------------------------- |
| `mitochondira~1` | `mitochondria`, `mitochondrial`, etc.              |
| `kubernates~2`   | `kubernetes`, `kubernates`, `kubernetes` variants. |

Use sparingly on short terms: `cat~2` matches almost everything.

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
| `page:[10 TO 20]`                           | Pages 10 to 20 inclusive.          |
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
| `*tion`   | Wildcard prefixes are not supported; anchor at the end only.  |

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

### Composing: worked examples

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

## Contributing

Bug reports and focused PRs are welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md)
for dev setup and the "Open with…" app-catalogue workflow.

## Security

fnd is local-only (no network, no telemetry). For the threat model and private
vulnerability reporting, see [`SECURITY.md`](SECURITY.md).

## Support

fnd is free and always will be. If it's earned a spot in your workflow and you feel like buying a broke student dev a coffee, the button's there.
Much gratitude if you do, but I hope you find the tool useful either way.

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/ben.dev.au)

## License

[MIT](LICENSE) © Ben Davidson

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
