# Sublime Text

Code editor with the `subl` CLI. Supports `path:line:column` deep-link.

```toml
[apps.sublime]
display_name = "Sublime Text"
handles      = ["md", "markdown", "txt", "*"]
argv         = ["subl", "{path}:{line}:1"]
```

## Setup

* Install the `subl` CLI: in Sublime, open the Command Palette and run
  `Install Package Control` (one-time), then ensure `subl` is on your
  PATH via `Sublime Text > Help > Install 'subl' command-line tool`
  (older versions: copy the symlink manually).
* When `{line}` is empty (PDF / PPTX / DOCX hits), the template's
  trailing `:1` collapses to nothing and Sublime opens the file at the
  top.

## Notes

* Sublime's `subl` CLI accepts `:line:column` syntax natively. No URL
  scheme needed.
* Drop the `:1` if you'd rather not pass a column.
