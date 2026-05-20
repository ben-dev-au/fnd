# App routing, per-source app association, position-aware open, clone source

Status: Draft → Implementing 2026-05-19
Branch: `feat/app-routing-and-source-app`

## Why

`o` today runs `fnd.opener.open_smart`, which knows one thing: PDFs with a
page locator get sent to Skim via `skim://`; everything else falls through to
macOS `open`. Four user-facing capabilities are missing:

1. **Pick which app a file type opens in.** No way to say "this .md opens in
   Obsidian, not VS Code" without editing LaunchServices.
2. **Tag a source as belonging to a specific app/vault.** Personal vaults
   (Obsidian) and code repos (VS Code) want different defaults per-source,
   not per-machine.
3. **Jump to the matched position in non-PDF files.** `Hit.heading_path` is
   indexed but never reaches an opener; MD/TXT have no `line` at all.
4. **Share a source between collections.** Today the only path is editing
   `config.toml` by hand and recreating the same `[[sources]]` block — fine
   in TOML, awful in the Settings TUI.

Coupling: items 1–3 share the same new abstraction (an apps registry), so
they ship together. Item 4 is independent but small enough to ride along.

## Scope

In:

* New `[apps]` and `[app_defaults]` config sections + pre-shipped registry
  (`system`, `preview`, `skim`, `pdf_expert`, `obsidian`, `vscode`).
* Per-source `app`, `app_for`, `app_params` fields.
* Refactor of `fnd/opener.py` to dispatch through the registry — Skim's URL
  output preserved bit-for-bit when Skim is the resolved app.
* `Hit.line` + `F_LINE` schema field + bump to `SCHEMA_VERSION = 7`.
  Markdown and plain-text extractors emit `chunk.line`.
* `O` shortcut → "Open with…" `ModalScreen` listing apps eligible for the
  focused hit's kind; resolved default highlighted; first-letter shortcuts.
* Settings TUI: per-source app picker, vault auto-detect with override,
  global per-filetype defaults, "Clone from another collection…" flow.
* Accessibility-permission UX: detect AX-not-trusted on first Preview
  AppleScript invocation, one-shot `notify()` with copy and System Settings
  pointer, fall back to plain `open <pdf>` until granted.
* Community catalogue at `docs/apps/` — one Markdown file per third-party
  app with a copy-pasteable `[apps.<id>]` TOML block. Settings TUI links to
  the GitHub directory from an "Add a custom app…" row.

Out:

* Cross-collection source **sharing** (one logical source backing multiple
  collections). Clone is deep-copy; sharing needs an opaque source-id
  refactor across the indexer — deferred.
* DOCX/PPTX line tracking. They carry `heading_path` / `slide`; few apps
  offer CLI-level deep links anyway.
* Non-macOS opener backends. argv templates work cross-platform in
  principle, but no Linux/Windows handlers ship in this branch.
* Obsidian Advanced URI auto-detection. Built-in `obsidian://open?` is the
  default; power users edit `[apps.obsidian]` themselves.
* AppleScript page-jump for non-PDF apps. Preview is the one ship-default
  where the complexity is worth it.

## Design

### 1. Apps registry (`fnd/apps.py`, new)

```python
@dataclass(frozen=True)
class OpenRequest:
    path: Path
    kind: str
    page: int = 0
    slide: int = 0
    heading_path: str = ""
    line: int = 0
    query: str = ""
    vault: str = ""
    file_in_vault: str = ""
    source_path: Path | None = None

@dataclass(frozen=True)
class App:
    id: str
    display_name: str
    handles: tuple[str, ...]
    handler: Callable[[OpenRequest], int]
    available: Callable[[], bool]
    positional: bool
    notes: str = ""
```

`BUILTIN_APPS` ships six entries:

| id           | handles       | positional | how                                                                    |
|--------------|---------------|------------|------------------------------------------------------------------------|
| `system`     | `("*",)`      | False      | `subprocess.run(["open", str(path)])`                                  |
| `preview`    | `("pdf",)`    | True       | embedded poll-and-keystroke AppleScript via `osascript`; falls back to `open -a Preview <path>` when `ax_trusted()` returns False |
| `skim`       | `("pdf",)`    | True       | `subprocess.run(["open", skim_url(path, page, search=query)])` — wraps existing `opener.skim_url()` |
| `pdf_expert` | `("pdf",)`    | True       | URL template `pdf-expert-7://open?url={path_pct}&page={page}` (verify against current docs at implementation time; ship commented if uncertain) |
| `obsidian`   | `("md", "markdown")` | True | URL template `obsidian://open?vault={vault_pct}&file={file_in_vault_pct}` with `%23{heading_pct}` appended to `file` when `heading_path` is non-empty. Requires `vault` |
| `vscode`     | `("md", "markdown", "txt", "*")` | True | argv `["code", "-g", "{path}:{line}:1"]`; line segment omitted when `line=0` (becomes `["code", str(path)]`) |

`resolve_app(*, kind, source, app_defaults, registry) → App` walks:

1. `source.app_for[kind]` if set and the id exists.
2. `source.app` if set and its `handles` covers `kind`.
3. `app_defaults[kind]` if set.
4. `system`.

`ax_trusted() → bool` checks `AXIsProcessTrustedWithOptions` via PyObjC if
available; otherwise probes with a no-op `osascript -e 'tell application
"System Events" to return name of first process'` and treats exit-code 1
with "not authorized" in stderr as untrusted. Cached per process.

`load_user_apps(cfg) → dict[str, App]` reads `[apps.<id>]` tables, builds
`App` records whose handler does pure-Python template substitution into
the configured `argv` or `url` form. argv tokens are substituted
independently then passed to `subprocess.run` as a list. URL templates go
through `["open", url]`. Variables: `{path}`, `{path_pct}`, `{page}`,
`{slide}`, `{line}`, `{heading}`, `{heading_pct}`, `{query}`,
`{query_pct}`, `{vault}`, `{vault_pct}`, `{file_in_vault}`,
`{file_in_vault_pct}`. Empty when the underlying field is unset.

### 2. Opener refactor (`fnd/opener.py`)

`skim_url`, `peek`, `reveal_in_finder`, `reveal` stay. `open_smart`
becomes:

```python
def open_smart(*, path, kind, page=0, query="", source=None, pdf_strategy=...):
    cfg = _cached_config()
    registry = build_registry(cfg)
    app = resolve_app(
        kind=kind,
        source=source,
        app_defaults=cfg.app_defaults,
        registry=registry,
    )
    req = OpenRequest(path=path, kind=kind, page=page, query=query, ...)
    return app.handler(req)
```

`pdf_strategy` is kept for back-compat but no longer drives Skim vs
LaunchServices directly — when callers pass `pdf_strategy="default"` the
opener forces `system` as the resolved app for that call.

`open_default(path)` keeps its current behavior (`open <path>`) and is the
handler the "System Default" menu row invokes.

`explain_open` returns `f"{app.id}: {repr(argv_or_url)}"`.

### 3. Config (`fnd/config.py`)

```python
class AppConfig(BaseModel):
    display_name: str
    handles: list[str]
    argv: list[str] | None = None
    url: str | None = None
    detect: Path | None = None  # auto-availability probe
    positional: bool = False
    requires: list[str] = []     # required OpenRequest fields
    notes: str = ""

    @model_validator(mode="after")
    def _exclusive(self) -> AppConfig:
        if (self.argv is None) == (self.url is None):
            raise ValueError("AppConfig: exactly one of argv or url")
        return self

class Config(BaseModel):
    defaults: Defaults = ...
    collections: dict[str, CollectionConfig] = ...
    ranking: dict[str, RankingProfileConfig] = ...
    apps: dict[str, AppConfig] = Field(default_factory=dict)
    app_defaults: dict[str, str] = Field(default_factory=dict)
```

`SourceConfig` gains three optional fields:

```python
app: str | None = None
app_for: dict[str, str] = Field(default_factory=dict)
app_params: dict[str, str] = Field(default_factory=dict)
```

A top-level `Config` `model_validator` walks every source and every
`app_defaults` entry, asserting each id resolves in `BUILTIN_APPS |
load_user_apps(self).keys()`.

`CONFIG_TEMPLATE` gains a commented `[app_defaults]` block and
`[apps.<id>]` examples for `skim`, `pdf_expert`, `obsidian`, `vscode`,
plus a pointer to `docs/apps/`.

### 4. Schema bump (`fnd/schema.py`, `fnd/query.py`, extractors)

`SCHEMA_VERSION = 7`. New `F_LINE = "line"` (u64, stored, indexed). `Hit`
gets `line: int = 0`.

Markdown and plain-text extractors track 1-based start-line per chunk.
Other extractors leave `line=0`. Indexer writes `doc.add_u64(F_LINE,
chunk.line)`. Decoder reads it back; absent (older indexes) defaults to 0.

Migration uses the existing `fnd/migrate.py:check_schema_status()` path —
sidecar mismatch fires the prompt-to-rebuild on next TUI launch.

### 5. Open-with modal (`fnd/tui/open_with_screen.py`, new)

`OpenWithScreen(ModalScreen[None])` constructed with `(hit, source)`.
Lists every `App` where `hit.kind in app.handles or "*" in app.handles`
AND `app.available()`. Resolved default highlighted. `Enter` fires it;
first-letter shortcut keys (`s` for skim, `p` for preview, `o` for
obsidian, etc.) fire individual rows; `Esc` dismisses. On select, builds
the `OpenRequest`, calls the handler, dismisses; non-zero exit surfaces a
`notify()` with stderr.

`O` rebinds from `open_default_app` to `open_with_menu` in
`fnd/tui/actions.py`. The old `action_open_default_app` stays as a method
(reachable via `:` palette) but has no default binding. Muscle-memory
preservation: `O` then `Enter` ≈ today's `O` (default-first-in-menu).

### 6. Vault auto-detect

`detect_obsidian_vault(path: Path) → str | None` walks up from `path`
looking for a `.obsidian/` directory; returns the basename of the first
hit. Used by `SourceFormScreen` when the user picks `obsidian` as the
source's app — pre-fills `app_params.vault` and lets the user override.

### 7. Settings TUI changes (`fnd/tui/settings_screen.py`, `fnd/tui/menu.py`)

* `SourceFormScreen`: new App picker row (KIND_PICKER over registry).
  When set to `obsidian` and no vault is set, auto-detects and pre-fills.
* `_provider_preferences` gains per-filetype default-app pickers
  (md, pdf, txt — picker over registered apps).
* `_provider_sources` gains "Clone from another collection…" row after
  "Add source".
* `_KEYS_RESULTS` rewritten: `o` description unchanged, `O` becomes
  "Open with… (menu of apps for this file type)".

### 8. Clone source (`fnd/config.py`, `fnd/tui/settings_screen.py`)

`clone_source(config_path, source_collection, source_index,
target_collection) → int`: reads TOML, deep-copies the table at
`[collections.<src>.sources][idx]`, appends to
`[collections.<target>.sources]`. Returns new index. Uses existing TOML
write primitives in `fnd/config.py`.

UI: `CloneSourcePickCollectionScreen` then `CloneSourcePickSourceScreen`
(both `ModalScreen`s following the `RenameCollectionScreen` pattern).
After confirm, triggers `_reindex_collection_async(target)`.

### 9. Community catalogue (`docs/apps/`)

* `README.md` — what the catalogue is, contribution guidelines, schema,
  variable list.
* `pdf-expert.md` — known-good or "needs verification" placeholder.
* Starter pack: `marked.md`, `typora.md`, `sublime-text.md` — illustrative
  examples for users to copy from.

Settings TUI gains an "Add a custom app…" row that opens
`https://github.com/<repo>/tree/main/docs/apps` in the browser.

### 10. Safety

* User TOML never reaches a shell. URL templates → `["open", url]`. argv
  templates substituted token-by-token, passed as list to `subprocess.run`
  without `shell=True`.
* `urllib.parse.quote` applied to every `_pct` variable.
* Preview AppleScript is embedded in `fnd/apps.py` as a constant string;
  user-supplied apps cannot inject arbitrary AppleScript (no `osascript`
  path in `load_user_apps`).
* Pydantic validators reject app ids outside `[A-Za-z0-9_-]{1,32}`;
  `handles` restricted to `{md, markdown, txt, pdf, pptx, docx, *}`.

## Phasing

0. Preview AppleScript spike (`scripts/spike_preview_page_jump.py`,
   deleted before merge). Gate: ≥9/10 success at <2s p95 → Preview becomes
   default for `pdf`; else Skim regains auto-default-when-present.
1. Apps registry + opener refactor + `docs/apps/`.
2. Per-source app + vault auto-detect.
3. Open-with modal.
4. Schema bump + MD/TXT line tracking.
5. Clone source.

Each phase: write tests first, get them green, run
`pre-commit run --all-files`, commit, move on.

## Verification

End-to-end after all phases:

1. Search a PDF. `o` opens Preview at the right page. `O` shows Preview
   (highlighted), Skim if installed, PDF Expert if installed, System
   Default.
2. Edit a source via Settings → set App = Obsidian. Vault pre-fills.
   Search a `.md`. `o` opens Obsidian at the matched heading. `O` lists
   Obsidian, VS Code, System Default.
3. Settings → Collections → Sources → Clone from another collection… →
   pick collection → pick source → confirm. New row appears, reindex
   prompt fires.
4. Revoke Accessibility for the launching process. `o` on a PDF → one-shot
   notify with System Settings path, Preview opens to page 1. Re-grant →
   next `o` jumps correctly.

Skim invariant: `tests/test_opener.py` snapshots the constructed URL for
the Skim path. Refactor must not change it.
