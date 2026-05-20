# Typora

WYSIWYG-style Markdown editor with inline rendering. Opens the file at
the top — no CLI deep-link to a heading or line.

```toml
[apps.typora]
display_name = "Typora"
handles      = ["md", "markdown"]
argv         = ["open", "-a", "Typora", "{path}"]
```

## Notes

* Typora is paid; install from typora.io.
* Set as the default `.md` opener globally with
  `[app_defaults] md = "typora"`.
* No deep-link support; use Obsidian or VS Code if you want the opener
  to land on the matched heading/line.
