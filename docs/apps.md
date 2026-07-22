# App catalogue

Config blocks for adding third-party apps to fnd's "Open with…" menu. Paste a
block into your `config.toml` (`fnd config path` prints the location) and the app
appears in the `O` picker. Built-in apps ship in `fnd/apps.py` and need no entry
here: **Skim, Preview, PDF Expert** (macOS), **Zathura, Okular** (Linux),
**SumatraPDF** (Windows), plus the cross-platform **Obsidian, VS Code, System
Default**. Each is offered only where it's installed (auto-detected per OS), so
the picker stays relevant on every machine.

Contributions welcome: add your app's block under [Apps](#apps) in a PR.

## Using an entry

1. Open your `config.toml` (`fnd config path` prints the location).
2. Paste the `[apps.<id>]` block.
3. Optionally set it as the default opener for a file type:
   ```toml
   [app_defaults]
   md = "marked"
   ```

## Template variables

Each handler is invoked with these substitutions. A placeholder for a field the
current hit lacks renders as an empty string.

| Variable              | Meaning                                               |
| --------------------- | ----------------------------------------------------- |
| `{path}`              | Absolute filesystem path.                             |
| `{path_pct}`          | URL-encoded `{path}`.                                 |
| `{page}`              | 1-based PDF page number; empty when unknown.          |
| `{slide}`             | 1-based PPTX slide number; empty when unknown.        |
| `{line}`              | 1-based source line; empty when unknown.              |
| `{heading}`           | Section heading path (e.g. `Intro/Goals`).            |
| `{heading_pct}`       | URL-encoded `{heading}`.                              |
| `{query}`             | Original search query.                                |
| `{query_pct}`         | URL-encoded `{query}`.                                |
| `{vault}`             | Obsidian vault name (from source `app_params.vault`). |
| `{vault_pct}`         | URL-encoded `{vault}`.                                |
| `{file_in_vault}`     | Path relative to the source root.                     |
| `{file_in_vault_pct}` | URL-encoded `{file_in_vault}`.                        |

Templates ending in `::col` or `::` (when `{line}` is empty) collapse to just
`{path}`, which is why `code -g {path}:{line}:1` works whether or not the hit
has a line locator.

## Schema

Every entry needs:

* `display_name`: what users see in the menu.
* `handles`: list of file kinds. Allowed: `md`, `markdown`, `txt`, `pdf`,
  `pptx`, `docx`, or `*` (any).
* Exactly one of:
  * `argv`: list of strings run via `subprocess.run(..., shell=False)`.
  * `url`: string handed to `open <url>`.

Optional `notes`: short freeform description, not shown to users today.

User TOML never reaches a shell: `argv` is passed as a list to `subprocess.run`,
and `url` is passed as a single argv element to `open`. The `_pct` variables
percent-encode every byte outside `A-Za-z0-9._~-`.

## Platform notes

fnd provides the *mechanism* — a data-driven registry plus the `[apps.<id>]`
config block — and ships a small, well-tested set of built-ins per OS. Anything
else is a paste-in config you own; good ones are welcome as a PR to the
catalogue below so other users on your platform get them too.

**Page-jump** works wherever the viewer exposes a page locator. The built-ins
that page-jump: Skim (`skim://…#page=N`) and Preview (macOS), Zathura and Okular
(`--page N`, Linux), SumatraPDF (`-page N`, Windows). Others open at the top of
the file. When no explicit `[app_defaults].pdf` is set, fnd auto-promotes the
first installed page-jump viewer for your OS; set `[app_defaults]` to override.

### Evince (Linux)

GNOME's document viewer. The example opens at the top; Evince's page CLI is
version-dependent, so page-jump is left out of the paste-in block.

```toml
[apps.evince]
display_name = "Evince"
handles      = ["pdf"]
argv         = ["evince", "{path}"]
```

### Xreader / Atril (Linux)

Cinnamon / MATE forks of Evince. Both accept `--page-label`:

```toml
[apps.xreader]
display_name = "Xreader"
handles      = ["pdf"]
argv         = ["xreader", "--page-label={page}", "{path}"]
```

### Default PDF viewer (Windows)

Open-only (no page locator). `cmd /c start "" {path}` hands the file to whatever
app Windows has registered for PDFs — that may be Acrobat, Edge, SumatraPDF, or
something else, so the entry is named for what it does, not a specific app. For a
page-jump-capable viewer, install SumatraPDF (a built-in — see the table above).

```toml
[apps.default_pdf]
display_name = "Default PDF viewer"
handles      = ["pdf"]
argv         = ["cmd", "/c", "start", "", "{path}"]
```

To target a specific app, point `argv` at its executable, e.g.
`["C:\\Program Files\\Adobe\\Acrobat DC\\Acrobat\\Acrobat.exe", "{path}"]`.

## Apps

### Marked 2

Markdown preview. Opens at the top of the file, no jump to a line or heading.

```toml
[apps.marked]
display_name = "Marked 2"
handles      = ["md", "markdown"]
argv         = ["open", "-a", "Marked 2", "{path}"]
```

### Sublime Text

Code editor. Jumps to `path:line:column`.

```toml
[apps.sublime]
display_name = "Sublime Text"
handles      = ["md", "markdown", "txt", "*"]
argv         = ["subl", "{path}:{line}:1"]
```

Needs the `subl` CLI on your PATH (Sublime Text > Help > Install 'subl'
command-line tool). When `{line}` is empty (PDF, PPTX, DOCX hits) the trailing
`:1` collapses and the file opens at the top.

### Typora

WYSIWYG Markdown editor. Opens at the top of the file, no line or heading jump.

```toml
[apps.typora]
display_name = "Typora"
handles      = ["md", "markdown"]
argv         = ["open", "-a", "Typora", "{path}"]
```

### PDF Expert

Built in as `pdf_expert`, so it needs no entry. Just set it as your PDF default:

```toml
[app_defaults]
pdf = "pdf_expert"
```

To override the built-in URL template, add your own block (entries here win on
id collision):

```toml
[apps.pdf_expert]
display_name = "PDF Expert"
handles      = ["pdf"]
url          = "pdf-expert-7://open?url={path_pct}&page={page}"
```

## Contributing

One app per PR: a sentence of purpose, the copy-paste `[apps.<id>]` block, and
any required setup (a CLI to install, a URL scheme to enable). Add it under Apps
above.
