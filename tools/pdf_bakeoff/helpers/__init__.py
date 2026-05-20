"""Long-running daemon helpers for the heavy ML extractors.

Each helper is a standalone Python script meant to be executed by the
*tool's own* Python interpreter (e.g.
`~/.local/share/uv/tools/marker-pdf/bin/python`) — not fnd's venv.
This sidesteps the dependency conflicts (typer<0.22 for docling,
pillow<11 for marker, pillow>=11 for mineru) that prevent any of these
from coexisting in one venv.

Protocol (stdin/stdout JSON lines):
  - on startup: write `{"_status": "ready"}` then read PDF paths
  - per request: read one PDF path from stdin
  - per response: write one JSON object to stdout:
      {"pdf": "...", "wall_ms": 1234.5, "md": "..."}  on success
      {"pdf": "...", "error": "..."}                    on failure
  - shutdown: read empty line or EOF
"""
