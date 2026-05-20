# PDF Expert (Readdle)

PDF Expert is pre-registered as the built-in `pdf_expert` app, so you
don't need a catalogue entry to use it — just set it as your PDF default:

```toml
[app_defaults]
pdf = "pdf_expert"
```

The built-in URL template is

```
pdf-expert-7://open?url={path_pct}&page={page}
```

This is a best-effort form documented for PDF Expert 7. If your version
uses a different scheme, override it by adding an `[apps.pdf_expert]`
table in your `config.toml` — entries here win against built-ins on id
collision.

## Override example

```toml
[apps.pdf_expert]
display_name = "PDF Expert"
handles      = ["pdf"]
url          = "pdf-expert://?path={path_pct}&page={page}"
```

## Verification

If `o` opens PDF Expert but lands on page 1 instead of the matched page:

1. Open Terminal and run:
   ```
   open "pdf-expert-7://open?url=file:///Users/you/Documents/test.pdf&page=5"
   ```
2. If PDF Expert opens at page 5, the built-in template is right for
   your install.
3. If PDF Expert opens at page 1 or doesn't open, try the variants in
   the override block above and submit a PR with whichever works.
