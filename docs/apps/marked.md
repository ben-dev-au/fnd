# Marked 2

Markdown preview app from Brett Terpstra. Renders the file with live
reload as the source on disk changes. No position support — Marked
always opens to the top of the document.

```toml
[apps.marked]
display_name = "Marked 2"
handles      = ["md", "markdown"]
argv         = ["open", "-a", "Marked 2", "{path}"]
```

Set as the default `.md` opener globally:

```toml
[app_defaults]
md = "marked"
```

Or per-source, via the Settings TUI (Phase 2).

## Notes

* Marked 2 is paid; install from the Mac App Store or marked2app.com.
* The bundle name is literally `Marked 2` (with the digit and space).
* No CLI deep-link to a heading or line. If you want headings, use
  Obsidian or VS Code.
