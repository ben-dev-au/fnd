# Apps catalogue

This directory collects user-contributed `[apps.<id>]` config blocks for
third-party apps that work well with `fnd`'s "Open with…" menu.

The catalogue is community-maintained. If you get an app working with a
deep-link / position template you find useful, open a PR adding a file
here. Built-in apps (Skim, Preview, Obsidian, VS Code, PDF Expert,
System Default) ship in `fnd/apps.py` and don't need a catalogue entry
unless you want to override their template.

## How to use a catalogue entry

1. Open your `config.toml` (find the path via `fnd config path`).
2. Paste the `[apps.<id>]` block at the bottom.
3. Optional: set it as the default for a filetype in `[app_defaults]`:
   ```toml
   [app_defaults]
   md = "marked"
   ```
4. Or attach it to a specific source via the Settings TUI (Phase 2).

## Template variables

Every handler is invoked with one of these per-token / per-URL
substitutions. Placeholders referencing fields the current hit doesn't
provide render as the empty string.

| Variable               | Meaning                                                |
|------------------------|--------------------------------------------------------|
| `{path}`               | Absolute filesystem path.                              |
| `{path_pct}`           | URL-encoded `{path}`.                                  |
| `{page}`               | 1-based PDF page number; empty when unknown.           |
| `{slide}`              | 1-based PPTX slide number; empty when unknown.         |
| `{line}`               | 1-based source line; empty when unknown.               |
| `{heading}`            | Section heading path (e.g. `Intro/Goals`).             |
| `{heading_pct}`        | URL-encoded `{heading}`.                               |
| `{query}`              | Original search query.                                 |
| `{query_pct}`          | URL-encoded `{query}`.                                 |
| `{vault}`              | Obsidian vault name (from source `app_params.vault`).  |
| `{vault_pct}`          | URL-encoded `{vault}`.                                 |
| `{file_in_vault}`      | Path relative to the source root.                      |
| `{file_in_vault_pct}`  | URL-encoded `{file_in_vault}`.                         |

Templates ending in `::col` or `::` (when `{line}` is empty) collapse to
just `{path}` — that's why `code -g {path}:{line}:1` works whether or
not the hit has a line locator.

## Schema

Every app entry needs:

* `display_name` — what users see in the "Open with…" menu.
* `handles` — list of file kinds. Allowed: `md`, `markdown`, `txt`,
  `pdf`, `pptx`, `docx`, or `*` (any).
* Exactly one of:
  * `argv` — list of strings exec'd via `subprocess.run(..., shell=False)`.
  * `url` — string handed to `open <url>`.

Optional:

* `notes` — short freeform description (shown in Settings TUI tooltips
  someday; not user-facing today).

## Safety

* User TOML never reaches a shell. `argv` is passed as a list directly to
  `subprocess.run`; `url` is passed as a single argv element to `open`.
* `_pct` variables are percent-encoded with every non-`A-Za-z0-9._~-`
  byte escaped — covers spaces, newlines, ampersands, hashes, quotes.

## Contributing

PRs welcome. One file per app, named after the app's id (kebab-case OK).
Include:

1. A two-sentence intro saying what the app is for.
2. The `[apps.<id>]` block, copy-pasteable.
3. Any setup the user needs to do (install the CLI, enable a URL scheme,
   grant a permission).
4. Known limitations (e.g. no page-jump, requires app already open).
