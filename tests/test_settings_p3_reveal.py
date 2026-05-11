"""Phase 3 (Settings UX redesign) — reveal & open-keybindings tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch


def test_reveal_runs_open_r_on_macos(tmp_path: Path) -> None:
    """Spec: Reveal-in-Finder — uses `open -R <path>` on macOS."""
    from acorn import opener

    p = tmp_path / "x.toml"
    p.write_text("")
    with patch.object(subprocess, "Popen") as mock_popen:
        opener.reveal(p)
        mock_popen.assert_called_once()
        args = mock_popen.call_args.args[0]
        assert args[0] == "open"
        assert args[1] == "-R"
        assert args[2] == str(p)
