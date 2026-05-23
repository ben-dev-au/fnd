"""One-time banner shown when fnd detects pre-upgrade PDF Texture Cache
entries and the texturising engine is now installed.

Lets the user re-texturise the affected PDFs in one batch instead of
discovering the regression silently when individual PDFs render flat
in the preview pane.

Dismissal persists per signature pair (old → current) so the user only
sees the banner once per real upgrade event - swapping back to the old
engine and forward again WILL re-show it, on purpose."""

from __future__ import annotations

import contextlib
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from platformdirs import user_data_dir
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


def _dismissed_path() -> Path:
    return Path(user_data_dir("fnd")) / "upgrade_banner_dismissed.toml"


def _load_dismissed() -> set[str]:
    path = _dismissed_path()
    if not path.exists():
        return set()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    pairs = data.get("dismissed", [])
    if not isinstance(pairs, list):
        return set()
    return {str(p) for p in pairs}


def _save_dismissed(pairs: set[str]) -> None:
    path = _dismissed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.write_text(tomli_w.dumps({"dismissed": sorted(pairs)}), encoding="utf-8")


def _pair_key(old_sig: str, current_sig: str) -> str:
    return f"{old_sig}>>{current_sig}"


def is_dismissed(old_sig: str, current_sig: str) -> bool:
    return _pair_key(old_sig, current_sig) in _load_dismissed()


def mark_dismissed(old_sig: str, current_sig: str) -> None:
    pairs = _load_dismissed()
    pairs.add(_pair_key(old_sig, current_sig))
    _save_dismissed(pairs)


def count_pre_upgrade_entries() -> tuple[int, str | None]:
    """Walk the on-disk PDF Texture Cache. Return ``(count, sample)``
    where ``count`` is the number of cache entries whose signature is
    different from the current ``extractor_signature()`` and ``sample``
    is one of those legacy signatures (None when count==0)."""
    try:
        from fnd.cache import default_cache_dir
        from fnd.extract.pdf import extractor_signature

        root = default_cache_dir()
        if not root.exists():
            return 0, None
        current = extractor_signature()
        sample: str | None = None
        n = 0
        for shard in root.iterdir():
            if not shard.is_dir():
                continue
            for entry in shard.glob("*.json"):
                _, _, sig = entry.stem.partition("--")
                if sig and sig != current:
                    n += 1
                    if sample is None:
                        sample = sig
        return n, sample
    except Exception:
        return 0, None


class UpgradeBannerScreen(ModalScreen[str]):
    """Modal banner offering Re-texturise now / later / dismiss.

    The chosen option name is passed to ``dismiss(...)`` so the caller
    can route through the existing Update-all flow without this screen
    importing the indexer machinery."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "dismiss_now", show=False),
        Binding("up,k", "cursor(-1)", show=False),
        Binding("down,j", "cursor(1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    CSS = """
    UpgradeBannerScreen { align: center middle; background: $surface 75%; }
    #upgrade_box {
        width: auto;
        min-width: 64;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: round $warning;
        padding: 0 1;
        background: $surface;
    }
    #upgrade_body { padding: 0 0 1 0; }
    #upgrade_list { height: auto; }
    """

    def __init__(self, *, n_entries: int, old_sig: str, current_sig: str) -> None:
        super().__init__()
        self._n_entries = n_entries
        self._old_sig = old_sig
        self._current_sig = current_sig

    def compose(self) -> ComposeResult:
        from rich.text import Text
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        n = self._n_entries
        body = Text()
        body.append(
            "PDF texturising has been upgraded. "
            f"{n} PDF{'s' if n != 1 else ''} "
            f"{'were' if n != 1 else 'was'} textured with the previous "
            "version - re-texturise them?\n\n"
        )
        body.append("Old signature  ", style="dim")
        body.append(self._old_sig)
        body.append("\n")
        body.append("New signature  ", style="dim")
        body.append(self._current_sig)
        with Vertical(id="upgrade_box") as box:
            box.border_title = "Texturising engine upgraded"
            yield Static(body, id="upgrade_body")
            yield OptionList(
                Option(
                    Text(
                        f"Re-texturise the {n} now",
                        style="bold green",
                    ),
                    id="now",
                ),
                Option("Re-texturise later from Settings", id="later"),
                Option("Dismiss (don't show again for this upgrade)", id="dismiss"),
                id="upgrade_list",
            )

    def on_mount(self) -> None:
        from textual.widgets import OptionList

        self.query_one("#upgrade_list", OptionList).focus()

    def action_cursor(self, direction: int) -> None:
        from textual.widgets import OptionList

        lst = self.query_one("#upgrade_list", OptionList)
        if direction > 0:
            lst.action_cursor_down()
        else:
            lst.action_cursor_up()

    def action_activate(self) -> None:
        from textual.widgets import OptionList

        self.query_one("#upgrade_list", OptionList).action_select()

    def action_dismiss_now(self) -> None:
        # Esc = "later" - non-destructive, banner re-appears next launch
        # until the user explicitly dismisses.
        self.dismiss("later")

    def on_option_list_option_selected(self, ev: Any) -> None:
        choice = str(ev.option.id) if ev.option.id else "later"
        if choice == "dismiss":
            mark_dismissed(self._old_sig, self._current_sig)
        self.dismiss(choice)


__all__ = [
    "UpgradeBannerScreen",
    "count_pre_upgrade_entries",
    "is_dismissed",
    "mark_dismissed",
]
