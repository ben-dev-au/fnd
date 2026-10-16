"""Section vectors beside the index: append-only segments, a manifest, and the row table.

A save writes one new segment and rewrites only the small manifest, so the first embed of a
large library never rewrites what it already wrote. Segments are named from a counter kept in
the manifest, so compaction cannot reuse a live name. Readers hold an immutable
:class:`Snapshot`; the TUI swaps in a new one by reference after each save. The
:class:`RowTable` maps each indexed section to its vector row as of one index commit.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from fnd import paths

if TYPE_CHECKING:
    import numpy as np

    from fnd.semantic import ModelSpec

__all__ = ["RECIPE", "RowTable", "Snapshot", "VectorStore", "store_dir"]

RECIPE: Final = "title+heading+body/256"
_MANIFEST: Final = "manifest.json"
_ROWS: Final = "rows.npz"


def store_dir(spec: ModelSpec) -> Path:
    return paths.app_data_dir() / "vectors" / f"{spec.name}-{spec.revision[:12]}"


def _replace_atomically(path: Path, write: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            write(f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_json(path: Path, data: object) -> None:
    _replace_atomically(path, lambda f: f.write(json.dumps(data).encode("utf-8")))


def _sweep(folder: Path, live: set[str]) -> None:
    """Delete segments the manifest no longer names; one still mapped on Windows waits."""
    for path in folder.glob("seg-*"):
        if path.name.split(".", 1)[0] not in live:
            with contextlib.suppress(OSError):
                path.unlink()


@dataclass(frozen=True, slots=True)
class Snapshot:
    vecs: np.ndarray
    key_row: dict[str, int]


class VectorStore:
    """Unit vectors keyed by section text, stored as float16 segments."""

    def __init__(self, folder: Path, dim: int, manifest: dict[str, Any], keys: list[str]) -> None:
        self.folder = folder
        self.dim = dim
        self._manifest = manifest
        self._keys = keys
        self._row = {k: i for i, k in enumerate(keys)}

    @classmethod
    def open(cls, folder: Path, *, dim: int, header: dict[str, Any]) -> VectorStore:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / _MANIFEST
        if path.is_file():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest.get("header") == header and manifest.get("dim") == dim:
                _sweep(folder, set(manifest["segments"]))
                keys: list[str] = []
                for seg in manifest["segments"]:
                    keys += json.loads((folder / f"{seg}.keys.json").read_text(encoding="utf-8"))
                return cls(folder, dim, manifest, keys)
            shutil.rmtree(folder)
            folder.mkdir(parents=True)
        manifest = {"header": header, "dim": dim, "segments": [], "next": 0}
        _write_json(path, manifest)
        return cls(folder, dim, manifest, [])

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self) -> list[str]:
        """Every key, in row order."""
        return list(self._keys)

    def has(self, key: str) -> bool:
        return key in self._row

    def append(self, keys: list[str], vecs: np.ndarray) -> None:
        import numpy as np

        if not keys:
            return
        seg = f"seg-{self._manifest['next']}"
        _replace_atomically(
            self.folder / f"{seg}.npy", lambda f: np.save(f, vecs.astype(np.float16))
        )
        _write_json(self.folder / f"{seg}.keys.json", keys)
        self._row.update({k: len(self._keys) + i for i, k in enumerate(keys)})
        self._keys += keys
        self._manifest["segments"].append(seg)
        self._manifest["next"] += 1
        _write_json(self.folder / _MANIFEST, self._manifest)

    def snapshot(self) -> Snapshot:
        import numpy as np

        parts = [
            np.load(self.folder / f"{s}.npy", mmap_mode="r") for s in self._manifest["segments"]
        ]
        vecs = np.concatenate(parts) if parts else np.zeros((0, self.dim), np.float16)
        return Snapshot(vecs=vecs, key_row=dict(self._row))

    def collect(self, live: set[str]) -> int:
        """Rewrite the store without unused rows once they pass a quarter; rows dropped."""
        dead = sum(1 for k in self._keys if k not in live)
        if dead * 4 <= len(self._keys):
            return 0
        snap = self.snapshot()
        keep = [k for k in self._keys if k in live]
        survivors = snap.vecs[[snap.key_row[k] for k in keep]]
        self._manifest["segments"] = []
        self._keys, self._row = [], {}
        self.append(keep, survivors)
        _sweep(self.folder, set(self._manifest["segments"]))
        return dead


@dataclass(frozen=True, slots=True)
class RowTable:
    """Each embedded section's vector row and memberships, as of one index commit."""

    parent_id: np.ndarray
    chunk_seq: np.ndarray
    vec_row: np.ndarray
    collections: tuple[str, ...]
    member: np.ndarray
    text_key: np.ndarray
    stamp: tuple[str, int, int]
    # Every opted-in section had a vector when this was written.
    complete: bool = True
    # A digest of each row's stored section, so an unchanged section reuses its key untokenised.
    stored_digest: np.ndarray | None = None

    def save(self, folder: Path) -> None:
        import numpy as np

        meta = json.dumps(
            {
                "collections": list(self.collections),
                "stamp": list(self.stamp),
                "complete": self.complete,
            }
        )
        arrays: dict[str, np.ndarray] = {
            "parent_id": self.parent_id,
            "chunk_seq": self.chunk_seq,
            "vec_row": self.vec_row,
            "member": self.member,
            "text_key": self.text_key,
            "meta": np.array([meta]),
        }
        if self.stored_digest is not None:
            arrays["stored_digest"] = self.stored_digest
        _replace_atomically(folder / _ROWS, lambda f: np.savez(f, **arrays))  # pyright: ignore[reportArgumentType]

    @classmethod
    def load(cls, folder: Path) -> RowTable | None:
        import numpy as np

        path = folder / _ROWS
        if not path.is_file():
            return None
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"][0]))
            return cls(
                parent_id=z["parent_id"],
                chunk_seq=z["chunk_seq"],
                vec_row=z["vec_row"],
                collections=tuple(meta["collections"]),
                member=z["member"],
                text_key=z["text_key"],
                stamp=(str(meta["stamp"][0]), int(meta["stamp"][1]), int(meta["stamp"][2])),
                complete=bool(meta.get("complete", False)),
                stored_digest=z["stored_digest"] if "stored_digest" in z.files else None,
            )
