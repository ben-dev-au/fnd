# FND

[![CI](https://github.com/ben-dev-au/fnd/actions/workflows/ci.yml/badge.svg)](https://github.com/ben-dev-au/fnd/actions/workflows/ci.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform: macOS | Linux (beta) | Windows (beta)](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20(beta)%20%7C%20Windows%20(beta)-lightgrey.svg)](#platform-support)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/ben.dev.au)

Fast, free, keyboard-driven document search for macOS. Indexes PDF, DOCX, PPTX,
MD and 60 different file types, with strong BM25 ranking, in-file navigation, an
"Open with…" launcher, and a lazygit-style TUI.

> **Linux and Windows are early beta — barely tested.** The code paths exist and
> the automated suite runs against all three OSes in CI, but fnd is developed and
> used daily on macOS only. Nobody has put real day-to-day use on the Linux
> build, and the Windows build has had essentially none at all. Expect rough
> edges, and please [open an issue](https://github.com/ben-dev-au/fnd/issues) when
> you hit one. See [Platform support](#platform-support) for what that means in
> practice.

## Status

On macOS, core features are complete and stable; the project is in a refinement
period of finding, fixing, and polishing. On Linux and Windows, treat everything
as unproven — see the note above.

## Requirements

- **macOS** (Apple Silicon or Intel) — the supported, tested platform.
- **Linux** or **Windows** — early beta, largely untested. See
  [Platform support](#platform-support).
- Nothing else to set up. Each install option below brings Python 3.13 with it.
- A modern terminal is recommended (see [Terminal compatibility](#terminal-compatibility)).

## Install

Pick one of the options below. They install the same tool, so you only need one.

### Option 1: Homebrew (macOS)

```sh
brew install ben-dev-au/tap/fnd
```

[Homebrew](https://brew.sh) is the standard macOS package manager. Install it
once, then run the line above. Apple Silicon installs a prebuilt binary; Intel
builds from source, which takes a few minutes.

### Option 2: uv or pipx (macOS, or Linux / Windows in beta)

```sh
uv tool install fndr        # or:  pipx install fndr
```

To run it once without installing: `uvx --from fndr fnd`.

---

Either way, launch the app by typing `fnd`.

> On PyPI the package is named `fndr` (`fnd` was taken); the command stays `fnd`.

Releases carry build provenance; see [`SECURITY.md`](SECURITY.md) to verify a download.

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

## Platform support

**macOS is the supported platform.** It is where fnd is developed, run daily, and
where every feature has been exercised by hand against a real corpus.

**Linux and Windows are early beta.** Platform-specific behaviour is isolated
behind four seams — where files live, how to open and reveal them, what the OS
calls things, and how cloud-backed files behave — and each has a Linux and
Windows implementation. The automated suite runs on `macos-14`, `ubuntu-latest`
and `windows-latest` in CI. That establishes the code runs; it does not establish
that the app is pleasant, correct, or even usable in daily work on those OSes,
because nobody has yet done that. Windows in particular has had almost no
hands-on use.

So read the table below as **what is implemented**, not as what is verified:

| Capability                        | macOS          | Linux (beta)                     | Windows (beta)     |
| --------------------------------- | -------------- | -------------------------------- | ------------------ |
| Maturity                          | tested in use  | CI only                          | CI only            |
| Search · indexing · TUI · preview | ✓              | ✓                                | ✓                  |
| Open in app / **Open with…**      | ✓              | ✓                                | ✓                  |
| Reveal in file manager (`R`)      | Finder         | file-manager `--select` → folder | Explorer `/select` |
| PDF page-jump on `o`              | Skim, Preview  | Zathura, Okular                  | SumatraPDF         |
| Structured PDF extra (docling)    | ✓              | ✓                                | ✓                  |
| Created-date filter               | ✓ (birth time) | best-effort (statx)              | ✓ (creation time)  |
| Cloud-only file handling          | iCloud Drive   | not detectable                   | OneDrive & co.     |
| Finder tag filtering              | ✓              | —                                | —                  |

Where an OS has no equivalent for something, fnd degrades rather than erroring.
Built-in PDF viewers are auto-detected — the "Open with…" picker lists only the
ones actually installed. On Linux/Windows, point `[app_defaults].pdf` at your
preferred viewer, or add any app with a small `[apps.<id>]` block (see
[`docs/apps.md`](docs/apps.md)); the bundled Linux and Windows handlers are
written from each app's documented CLI and have not been confirmed against a
live install. **Frontmatter tags** (`tags:` in YAML) work on every OS; **Finder
tags** are macOS-only. Install with `uv`/`pipx` on any OS; Homebrew is
macOS-only.

If you run fnd on Linux or Windows, reports are genuinely valuable — including
"it worked fine", which is the datapoint the table is currently missing.

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

| Key            | What it does                                                                                                                                                                                                                                       |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `↑` / `↓`      | Move the cursor up/down through results (vim's `k` / `j` also work).                                                                                                                                                                               |
| `⌥↑` / `⌥↓`    | **Skim**: hold Option (Alt) and arrow to move through results _without_ loading each preview — browse fast with no per-row mount/lag. The preview loads again on a normal `↑`/`↓` (the row you land on) or `Enter` (the exact row you skimmed to). |
| `Enter`        | Load the highlighted result into the preview (handy right after an Option-skim).                                                                                                                                                                   |
| `→`            | Expand the focused file to its matching sections; press again to drill into the first.                                                                                                                                                             |
| `←`            | Collapse the focused node, or back out to its parent (lazygit-style).                                                                                                                                                                              |
| `Ctrl→` / `⌥→` | **Expand all**: expand the focused node _and its whole subtree_ (results, collections and filters trees).                                                                                                                                          |
| `Ctrl←` / `⌥←` | **Collapse children**: fold away every descendant, keeping the node itself open.                                                                                                                                                                   |
| `Tab`          | Cycle focus between the query bar, the results tree, and the preview.                                                                                                                                                                              |
| `/`            | Jump back to the query bar to refine your search.                                                                                                                                                                                                  |
| `↑` / `↓`      | When the preview pane is focused, scroll the preview.                                                                                                                                                                                              |

> **Option-skim on Apple Terminal:** for `⌥↑` / `⌥↓` to reach fnd, enable
> _Settings → Profiles → Keys → Left Option key → Esc+_. iTerm2 and most modern
> terminals work without any change.
>
> **Expand/collapse-all — Ctrl or Option?** These are bound to _both_ `Ctrl` and
> `Alt`+arrow, because a single physical combo reaches the app under different
> names per terminal. On macOS, `⌃←`/`⌃→` are usually captured by Mission
> Control ("Move a space"), so use **`⌥←`/`⌥→`** — your terminal forwards it as
> whichever of the two the app has bound. On Windows/Linux, `Ctrl`+arrow works
> directly.

### Opening and acting on a result

| Key            | What it does                                                                                                                                                                       |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `o`            | Open the hit in its resolved app, jumping to the matching page / slide / line / heading.                                                                                           |
| `O`            | **Open with…**: a picker of every app that handles this file type. Use `↑↓` then `Enter`, or press the letter shown next to an app; `Esc` cancels.                                 |
| `R`            | **Reveal**: show the file in your file manager (Finder on macOS, File Explorer on Windows) with it selected, without opening it — also on the last row of the `O` picker. |
| `Space`        | Quick Look the file.                                                                                                                                                               |
| `:`            | Open the **Settings & Commands** menu: every setting and action in one searchable, full-screen list.                                                                               |
| `?`            | Keybindings cheat sheet (press again to dismiss).                                                                                                                                  |
| `Ctrl+F`       | Toggle auto-fuzzy matching (persists to your config).                                                                                                                              |
| `h`            | Toggle search-term highlighting in the preview.                                                                                                                                    |
| `w`            | Warm the focused file completely, so scrolling anywhere in it is instant. Asks first on a large file; press again on that file to stop.                                                          |
| `q` / `Ctrl+C` | Quit. `Esc` backs out of any overlay or nested screen.                                                                                                                             |

Inside the Settings menu (`:`) navigate with `↑↓` (or `j`/`k`), press `Enter` to
open / edit / toggle the focused row, `/` to filter rows by label, and `Esc` or
`←` to step back.

### Terminal compatibility

A modern terminal is required for optimal formatting. For example:

- **macOS**: iTerm2, Ghostty, Kitty, WezTerm. Terminal.app works but its
  formatting varies by font (Menlo is a reasonable default); a modern terminal
  is strongly suggested.
- **Linux**: Kitty, WezTerm, Alacritty, GNOME Terminal, or Konsole should all be
  fine — expected to work, not yet confirmed.
- **Windows**: **Windows Terminal** (bundled with Windows 11) or WezTerm. The
  legacy `conhost.exe` console renders box-drawing and colours poorly. Untested;
  if the TUI looks wrong, the terminal is the first thing to change.

## Command reference

| Command                                         | What it does                                                                                           |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `fnd`                                           | Launch the interactive TUI.                                                                            |
| `fnd <query>`                                   | Launch the TUI with `<query>` pre-filled.                                                              |
| `fnd -c <collection> <query>`                   | Launch the TUI scoped to a collection (`-c all` for every collection).                                 |
| `fnd tui [query]`                               | Explicitly launch the TUI (optional seed query).                                                       |
| `fnd search "<query>"`                          | Terminal search. Flags: `--limit`, `-c/--collection`, `--meta`, `--explain N`, plus the filters below. |
| `fnd index <root>`                              | Ad-hoc index a single root into the default collection.                                                |
| `fnd collection list`                           | List configured collections and their sources.                                                         |
| `fnd collection add <name>`                     | Add (or extend) a collection in the config TOML.                                                       |
| `fnd collection reindex <name>`                 | Index or re-index a configured collection (`--rebuild` to start fresh).                                |
| `fnd config show`                               | Print the effective merged config as JSON.                                                             |
| `fnd config path`                               | Print the path to the config TOML.                                                                     |
| `fnd config edit`                               | Open the config TOML in `$EDITOR` (creates a template if missing).                                     |
| `fnd config validate`                           | Validate the config TOML.                                                                              |
| `fnd extras list`                               | List optional extras and their installed status.                                                       |
| `fnd extras status`                             | Show installed extras with disk usage.                                                                 |
| `fnd extras install <name>`                     | Install an extra after a disk-impact disclosure prompt.                                                |
| `fnd extras uninstall <name>`                   | Remove an extra (indexed chunks remain).                                                               |
| `fnd cache status` / `info` / `prune` / `clear` | Manage the PDF extraction cache.                                                                       |
| `fnd version`                                   | Print the fnd version.                                                                                 |

### Search filters

`fnd search` takes the same filters as the TUI's Filters pane:

| Flag                   | Meaning                                                   |
| ---------------------- | --------------------------------------------------------- |
| `--tag <name>`         | Only files carrying this tag. Repeatable.                 |
| `--not-tag <name>`     | Exclude files carrying this tag. Repeatable.              |
| `--tag-match all\|any` | Combine multiple `--tag`s. Default `all`.                 |
| `--created <window>`   | Created within `today`/`yesterday`/`week`/`month`/`year`. |
| `--modified <window>`  | Modified within the same windows.                         |
| `--kind <ext>`         | Restrict to `pdf`/`docx`/`pptx`/`md`/`txt`. Repeatable.   |

```bash
fnd search "risotto" --tag recipe --not-tag draft --created month
```

Tags come from Obsidian-style YAML frontmatter (`tags:`) and, on macOS, from
Finder tags. Nested tags work as a hierarchy — `--tag project` also matches
`project/alpha`. Tag matching is case-insensitive, and a leading `#` is
optional. Which sources are read is set by `defaults.tag_sources` in the config
TOML; disabling one takes effect immediately, with no re-index.

Created dates come from the filesystem's creation time — macOS birth time,
Windows creation time, and statx-capable Linux filesystems (e.g. ext4). Files
without one match only the default (unfiltered) window. Only the macOS path has
been exercised against a real corpus.

## Open with… apps

In the TUI, `o` opens a hit in its resolved app and `O` opens the **Open with…**
picker. Built-in handlers ship per OS — **Skim, Preview, PDF Expert** (macOS),
**Zathura, Okular** (Linux, beta), **SumatraPDF** (Windows, beta), plus
cross-platform **Obsidian, VS Code, System Default** — and each is offered only
where it's installed. The non-macOS handlers are written from each app's
documented command line and have not been confirmed against a live install. Where the app and file type allow it, fnd jumps to the matching page,
slide, line, or heading. Set a per-file-type default with `[app_defaults]`, or a
per-source app, in your config.

You can add your own apps with a small `[apps.<id>]` block in your config; see
the catalogue and schema in [`docs/apps.md`](docs/apps.md). User templates are passed
to apps as argv lists (never a shell) or as percent-encoded URLs handed to
`open`, so file paths can't inject commands.

## Collections & sources

A **collection** is a named group of source folders you search together; each
**source** is a folder plus the include/exclude globs that decide which files in
it get indexed. Out of the box fnd searches **all** your collections; tick
individual ones in the sidebar to narrow that, and the selection is remembered
for next launch. `-c <name>` scopes a single launch, and `-c all` widens it back
out again without touching what's remembered.

There are three ways to manage collections, and they're interchangeable, because
the UI writes the same config file you can edit by hand.

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

# Narrow it with globs, or a markdown frontmatter filter
fnd collection add notes --source ~/Notes --include "**/*.md" --exclude "drafts/**"
fnd collection add notes --source ~/Vault --filter "NOT ('private' in tags)"

fnd collection list             # show what's configured
fnd collection reindex papers   # build/update the index (--rebuild to start fresh)
```

### From the config file

Run `fnd config edit` to open the TOML in `$EDITOR` (the first run writes a
commented starter template), then `fnd config validate` to check it. UI edits
preserve your comments and formatting, so hand-editing and the Settings UI mix
freely.

### Keeping files out of the index

Filters at index time decide what gets indexed at all, as opposed to the
Filters pane, which narrows what an already-built index returns. They live in
`[defaults.filters]` and, per source, in a `[…sources.filters]` table that
overrides the defaults field by field. Settings → **Indexing & PDF Texture →
Index filters** edits the defaults; a source's own **Index filters** row edits
its overrides.

| Filter | What it does |
| --- | --- |
| `respect_gitignore` | Honours every `.gitignore` down the tree, with git's rules — negation, directory patterns, nearest file wins. **On by default.** |
| `respect_fndignore` | The same syntax in a `.fndignore`, read only by fnd — how to hide something from search without hiding it from git. **On by default.** |
| `exclude_tags` | Tags that keep a file out. **macOS Finder tags only**, because reading a tag is one attribute lookup while reading a note's YAML tags means opening every candidate file. Defaults to `["no_index"]`. |
| `kinds` | Restrict to given file types. Empty means every supported type. |
| `min_size` / `max_size` | Bytes. Keeps stubs, and multi-hundred-megabyte scans, out. |
| `created_after` / `created_before` | ISO dates (`2024-01-01`). A fixed bound, not the Filters pane's rolling window — a window would change what the index holds as time passed. A file with no creation date (best-effort on Linux) is kept. |
| `modified_after` / `modified_before` | ISO dates, same semantics. |
| `frontmatter` | A frontmatter predicate — the same `[…]` syntax as a query. **Notes only**; every other file type passes through. Use this to exclude on a YAML tag: `NOT ('no_index' in tags)`. |
| `expression` | A predicate over any file, using `file.kind`, `file.size`, `file.modified`, `file.tags.os`, `file.path` and the like. The rows above are written in terms of it. |

Each screen has an **Edit as text** row showing the whole set as one
expression. The rows and the text are two views of the same filter, so editing
either updates the other — typing `file.kind in ['pdf']` fills the file-type
row in, and ticking that row writes the same clause back out. Anything the
rows cannot express stays in `expression` verbatim. A per-source row set to
`-` overrides to *nothing*, which is how one source opts out of a global
exclusion; leaving a row empty inherits instead.

Enabling a filter does not need a rebuild: the next update prunes anything
newly excluded. Two things worth knowing before you turn `respect_gitignore`
off and on:

> **A `.gitignore` says what git should not track, which is not always what you
> want unsearchable.** Large PDFs — textbooks, lecture slides — are commonly
> kept out of a repository precisely because they are big, and those are often
> exactly the documents you want to find.

> **Finder tags are macOS-only.** On Linux and Windows `exclude_tags` is inert;
> `.fndignore` and the `frontmatter` filter are the portable equivalents.

## Configuration

The config lives in your platform's app-data directory (run `fnd config path` to
see the exact location): `~/Library/Application Support/fnd/` (macOS),
`~/.local/share/fnd/` (Linux), or `%LOCALAPPDATA%\fnd\` (Windows). fnd also reads
`~/.config/fnd/config.toml` on any OS if you keep it there. `fnd config show`
prints the effective merged config; `fnd config validate` checks it before you
rely on it.

Each collection is one or more `[[collections.<name>.sources]]` tables. A
minimal, annotated config:

```toml
[defaults]
collection    = "all"      # scope a fresh profile starts with: "all" or a name
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
frontmatter_filter = "Status == 'published' AND NOT ('private' in tags)"  # md only

# Index-time filters every source inherits. A source's own [filters] table
# overrides these field by field; anything left out is inherited.
[defaults.filters]
respect_gitignore = true            # honour .gitignore, with git's own rules
respect_fndignore = true            # same syntax, read only by fnd
exclude_tags      = ["no_index"]    # macOS Finder tags — see the note below
# max_size        = 50_000_000
# modified_after  = 2020-01-01
# expression      = "file.size < 50_000_000"

# Default app per file type for the `o` shortcut. Built-in ids:
# system, obsidian, vscode (all OSes); skim, preview, pdf_expert (macOS);
# zathura, okular (Linux); sumatra (Windows).
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
It is installed via [uv](https://docs.astral.sh/uv/) (see the uv docs for the
one-line installer on your OS).

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
Noncommercial 1.0) and `docling-slim[standard]` (Apache-2.0). fnd redistributes
neither — the install fetches them onto your machine, so Polyform's
non-commercial restriction binds your use of `pymupdf-layout` directly. Check it
before installing this extra in a commercial setting. ML weights (~400 MB)
download on first use. Uninstall removes the packages; indexed structured chunks
remain in the index until the next reindex.

### Cost on first reindex

~30 s per PDF on M1 Max (pymupdf4llm; longer for pages routed through the docling
fallback). **A 200-book corpus is roughly a 2-hour one-time cost.** Subsequent
reindexes only re-process changed files.

### Cache

Extracted chunks are content-addressed in fnd's cache directory —
`~/Library/Caches/fnd/pdf-structure/` on macOS, the platform cache dir elsewhere
(`fnd cache info` prints it). Shared across collections: the same file in two
collections is extracted once.

In the TUI: **Settings → Indexing → Cache size** shows entries + disk;
**Cache maintenance…** drills to Prune stale (recoverable) and Clear
(destructive, confirms with `⚠ Cannot be undone`).

From the CLI: `fnd cache status / info / prune / clear`.

### Auto-resume on launch

A Ctrl+C, sleep, terminal close, or fnd quit during reindex leaves the cache and
a state file in fnd's data directory at `reindex/<collection>.state.toml`.

Reopen the TUI and indexing auto-resumes silently in the background.
Already-cached files return in milliseconds, so resume effectively starts where
you left off.

Toggle off from **Settings → Indexing → Auto-resume on launch**, or set
`defaults.indexer_auto_resume = false` in your config.

## Search how-to

fnd's query bar accepts plain words, phrases, boolean expressions, fuzzy and
proximity matches, wildcards and regex, field qualifiers, date filters, and
markdown frontmatter filters. They compose freely.

### The basics

| You type                      | What it does                                                                               |
| ----------------------------- | ------------------------------------------------------------------------------------------ |
| `entropy`                     | One term. Searches body, title, headings, and filename. Stemmed (`entropy` = `entropies`). |
| `cross entropy loss`          | Several terms, ranked. Docs matching more terms rank higher; all-term docs reach the top.  |
| `cross AND entropy`           | Require both terms.                                                                        |
| `"cross entropy loss"`        | Exact phrase, in order. Also matches `cross-entropy loss`.                                 |
| `cross OR entropy`            | Either term.                                                                               |
| `entropy NOT regression`      | Has `entropy`, excludes `regression`.                                                      |
| `+rust -python`               | `+` require, `-` exclude (shorthand for `AND` / `NOT`).                                    |
| `(loss OR cost) AND function` | Group with parentheses, to any depth.                                                      |

### Phrases

Quoting is the biggest precision win — quote any common phrase:

| You type              | Matches                                                              |
| --------------------- | -------------------------------------------------------------------- |
| `man in the middle`   | The four words anywhere in a chunk. Noisy.                           |
| `"man in the middle"` | The four words together, in order. Also matches `man-in-the-middle`. |

### Proximity

Find terms near each other, in any order — `{N}` and `NEAR/N` are equivalent:

| You type                           | Means                                        |
| ---------------------------------- | -------------------------------------------- |
| `{5} cross entropy`                | The two terms within 5 tokens of each other. |
| `cross NEAR/5 entropy`             | Same.                                        |
| `{20} man in the middle attack`    | All five words within ~one line of text.     |
| `{60} buffer overflow exploit`     | Within ~a few lines.                         |
| `{500} race condition mitigations` | Within ~one page.                            |

- **Scale:** `5` ≈ very near, `20` ≈ a line, `60` ≈ a few lines, `500` ≈ a page.
- **Order doesn't matter** — quote (`"cross entropy"`) when it does.
- `{N}` covers the words right after it, up to the first operator, `(`, or
  filter, so `{10} buffer overflow kind:pdf` slops only `buffer overflow`.
- Can't cross a chunk boundary; if terms are far apart, drop the `{N}`.
- `NEAR/N` takes exactly two words.

### Fuzzy matching for typos and variants

Suffix `~1` or `~2` to allow that many edits per term — an adjacent transposition
(`ir` ↔ `ri`) counts as one edit:

| You type         | Matches                               |
| ---------------- | ------------------------------------- |
| `mitochondira~1` | `mitochondria`, `mitochondrial`, etc. |
| `kubernates~2`   | `kubernetes` and near spellings.      |

Works on a single term or alongside others (`powerhouse mitochondira~1`). Use
sparingly on short terms: `cat~2` matches almost everything.

### Field qualifiers

A field qualifier is a hard filter: it narrows the result set (it does not just
boost), so you can combine it with search terms to constrain them.

| You type                   | What it does                                           |
| -------------------------- | ------------------------------------------------------ |
| `title:transformer`        | Only documents whose title contains `transformer`.     |
| `heading_path:"chapter 4"` | Only sections under that heading path.                 |
| `author:dijkstra`          | Only documents with that author metadata.              |
| `kind:pdf`                 | Only a file type (`pdf`, `docx`, `pptx`, `md`, `txt`). |
| `path_tokens:thesis`       | Only paths containing `thesis`.                        |
| `title:(rust OR golang)`   | Group alternatives within one field.                   |
| `has:author`               | Only documents that have a non-empty `author` field.   |

Combine with terms to constrain them — `kind:pdf "diffusion model"` finds the
phrase in **PDFs only**.

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

### Wildcards and regex

| You type   | Matches                                      |
| ---------- | -------------------------------------------- |
| `crypto*`  | Words starting with `crypto`.                |
| `gr?y`     | `?` = exactly one character: `gray`, `grey`. |
| `/cryp.*/` | A regular expression over indexed words.     |

> **`*` only works at the end of a word.** Leading or infix wildcards (`*tion`,
> `de*ce`) match almost nothing — search strips word endings before matching.
> Use a trailing `crypto*` or a `/regex/` instead.

You rarely need `*`: search already matches word variants (`entropy` finds
`entropies`). Wildcards, fuzzy, regex, and phrases all work inside
`AND` / `OR` / `NOT` / `()`.

### Markdown frontmatter filter

Filter markdown notes by their YAML frontmatter with a `[…]` predicate. The
same expression is also a source's `frontmatter_filter` in the config.
**String values use single quotes**; double quotes mark a field name with
spaces (`"Due Date"`):

| You type                                                | What it does                                          |
| ------------------------------------------------------- | ----------------------------------------------------- |
| `mitm [Course == 'Security Foundations']`               | Notes where the `Course` field equals that value.     |
| `[Notes_Type == 'Lecture' OR Notes_Type == 'Tutorial']` | Either value (there are no list literals — use `OR`). |
| `entropy [Course == 'ML' AND Year >= 2024]`             | Compound predicate.                                   |
| `['urgent' in tags]`                                    | `urgent` is an element of the `tags` list.            |
| `[NOT ('private' in tags)]`                             | Exclude a tag, **keeping notes that have no `tags:`**. |
| `[Course ~~ 'Design *']`                                | Glob a string value (not the body-search `~N` fuzzy). |
| `["Due Date" < 2026-01-01]`                             | A field name with a space, double-quoted.             |

Operators: `==` `!=` `<` `<=` `>` `>=` `~~` (glob, string fields), `in` /
`not in` (list membership), `AND`, `OR`, `NOT`, parentheses. Values are
single-quoted strings, numbers, ISO dates, or `true`/`false`/`null`. Only
markdown is filtered; other kinds pass through.

> **A missing field fails every comparison, including negative ones.** On a
> note with no `tags:`, `['x' not in tags]` is *false*, so the note is dropped;
> `[NOT ('x' in tags)]` is *true*, so it is kept. To exclude a tag without also
> losing untagged notes, negate the whole test: `NOT (… in …)`. The same rule
> makes `[Status != 'draft']` skip notes that have no `Status` at all.
>
> `in` needs a real YAML list. If some notes write `tags: private` as a bare
> string, add `AND NOT (tags ~~ '*private*')` to catch them too.

### Composing: worked examples

```text
"buffer overflow"                                  # exact phrase
{10} buffer overflow exploit kind:pdf              # three terms within 10 tokens, PDFs only
c:notes mitm [Course == 'Security Foundations']    # term + collection scope + frontmatter filter
title:"chapter 4" heading_path:proof               # constrain to one chapter's proofs
kind:pptx slide:>10 attention                      # later-half slides mentioning attention
mtime:month crypto*                                # recently-modified docs mentioning crypto-anything
crypto* AND wallet                                 # a wildcard required inside a boolean
(loss OR cost) AND function~1                      # grouping with a fuzzy term
"defence in depth" OR diverse                      # an exact phrase OR a loose term
```

### A few common pitfalls

- **Quoting a single word does nothing useful.** `"entropy"` is the same as
  `entropy`. Quotes only help for multi-word phrases.
- **`OR` and `AND` are case-sensitive.** Lowercase `or` / `and` are treated
  as ordinary terms. Always uppercase boolean operators.
- **Standalone stopwords are dropped.** `the man` searches just `man` — common
  words (`the`, `in`, `of`, …) are removed from unquoted queries. To match a
  phrase that includes them, quote it: `"man in the middle"`.
- **Proximity is per-chunk.** A phrase or `{N}` query can't span a chunk
  boundary. If the terms are paragraphs apart, drop to a loose multi-term query.
- **`*` only works at the end of a word.** Leading/infix wildcards (`*tion`,
  `de*ce`) match almost nothing — use `crypto*` or `/regex/`.

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

[GNU AGPL-3.0-or-later](LICENSE) © 2026 Ben Davidson

Use it, read it, modify it, run it — privately, for any purpose, with no
obligations at all. If you distribute it, or run a modified version as a network
service, that version has to ship its source under this same licence. Put
plainly: your use is either private, or it is open source.

fnd links [PyMuPDF](https://pymupdf.readthedocs.io/) (AGPL-3.0-or-commercial),
so any distributed combination already carried AGPL terms; this licence states
what was true of fnd all along.

Releases up to and including `fndr` 0.0.5 were published under MIT and remain
MIT.

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
