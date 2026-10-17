"""Section vectors beside the index: append-only segments, a manifest, and the row table.

One process writes at a time: :meth:`VectorStore.open_writer` holds an OS lock on ``writer.lock``,
which ends with the process however it ends. Readers never delete or rewrite anything.
A save writes one new segment and rewrites only the small manifest. Compaction renumbers rows and
bumps the manifest's ``generation``; a :class:`RowTable` filed under another generation is stale.
After a run the segments merge into one, which readers memory-map rather than copy.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Final

from fnd import paths

if TYPE_CHECKING:
    import numpy as np

    from fnd.query import Hit
    from fnd.semantic import ModelSpec

__all__ = [
    "RECIPE",
    "RowTable",
    "Snapshot",
    "StoreBusyError",
    "VectorStore",
    "store_dir",
    "store_header",
    "stored_digest",
]

RECIPE: Final = "title+heading+body/256"
_MANIFEST: Final = "manifest.json"
_ROWS: Final = "rows.npz"
_LOCK: Final = "writer.lock"
_READ_ATTEMPTS: Final = 5


class StoreBusyError(RuntimeError):
    """Another process is writing the vector store."""


def store_dir(spec: ModelSpec) -> Path:
    return paths.app_data_dir() / "vectors" / f"{spec.name}-{spec.revision[:12]}"


def store_header(spec: ModelSpec) -> dict[str, Any]:
    """What the vectors were made with; any change means they are re-embedded."""
    return {
        "model": spec.name,
        "revision": spec.revision,
        "max_tokens": spec.max_tokens,
        "recipe": RECIPE,
    }


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


def _acquire(folder: Path) -> IO[bytes]:
    handle = (folder / _LOCK).open("a+b")
    if not paths.try_lock(handle):
        handle.close()
        raise StoreBusyError(str(folder))
    return handle


def stored_digest(h: Hit) -> str:
    """The section's stored bytes, hashed without decoding them."""
    import hashlib

    d = hashlib.blake2b(digest_size=16)
    for part in (h.title.encode(), h.heading_path.encode(), h.stored_struct, h.body_md.encode()):
        d.update(len(part).to_bytes(8, "little"))
        d.update(part)
    return d.hexdigest()


@dataclass(frozen=True, slots=True)
class Snapshot:
    vecs: np.ndarray
    key_row: dict[str, int]


class VectorStore:
    """Unit vectors keyed by section text, stored as float16 segments."""

    def __init__(
        self,
        folder: Path,
        dim: int,
        manifest: dict[str, Any],
        keys: list[str],
        lock: IO[bytes] | None,
    ) -> None:
        self.folder = folder
        self.dim = dim
        self._manifest = manifest
        self._keys = keys
        self._row = {k: i for i, k in enumerate(keys)}
        self._lock = lock

    @classmethod
    def open(cls, folder: Path, *, dim: int, header: dict[str, Any]) -> VectorStore:
        """For reading: an empty store when absent or made with another header."""
        for attempt in range(_READ_ATTEMPTS):
            try:
                manifest = json.loads((folder / _MANIFEST).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break
            if manifest.get("header") != header or manifest.get("dim") != dim:
                break
            try:
                return cls(folder, dim, manifest, _read_keys(folder, manifest), None)
            except FileNotFoundError:
                if attempt == _READ_ATTEMPTS - 1:
                    raise
                time.sleep(0.05)
        return cls(folder, dim, _empty(header, dim), [], None)

    @classmethod
    def open_writer(cls, folder: Path, *, dim: int, header: dict[str, Any]) -> VectorStore:
        """For writing, as the only writer; raises :class:`StoreBusyError` when another writes."""
        folder.mkdir(parents=True, exist_ok=True)
        lock = _acquire(folder)
        try:
            path = folder / _MANIFEST
            manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
            if manifest is None or manifest.get("header") != header or manifest.get("dim") != dim:
                for child in folder.iterdir():
                    if child.name != _LOCK:
                        shutil.rmtree(child) if child.is_dir() else child.unlink()
                manifest = _empty(header, dim)
                _write_json(path, manifest)
            manifest.setdefault("generation", 0)
            _sweep(folder, set(manifest["segments"]))
            return cls(folder, dim, manifest, _read_keys(folder, manifest), lock)
        except BaseException:
            paths.unlock(lock)
            lock.close()
            raise

    def close(self) -> None:
        """Release the writer's lock."""
        if self._lock is not None:
            paths.unlock(self._lock)
            self._lock.close()
            self._lock = None

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def generation(self) -> int:
        return int(self._manifest.get("generation", 0))

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self) -> list[str]:
        """Every key, in row order."""
        return list(self._keys)

    def has(self, key: str) -> bool:
        return key in self._row

    def _writing(self) -> None:
        if self._lock is None:
            raise RuntimeError("store opened for reading")

    def append(self, keys: list[str], vecs: np.ndarray) -> None:
        import numpy as np

        if not keys:
            return
        self._writing()
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

        for attempt in range(_READ_ATTEMPTS):
            try:
                parts = [
                    np.load(self.folder / f"{s}.npy", mmap_mode="r")
                    for s in self._manifest["segments"]
                ]
                break
            except FileNotFoundError:
                # A writer merged or compacted since this reader opened; read its new manifest.
                if self._lock is not None or attempt == _READ_ATTEMPTS - 1:
                    raise
                fresh = VectorStore.open(self.folder, dim=self.dim, header=self._manifest["header"])
                self._manifest, self._keys, self._row = fresh._manifest, fresh._keys, fresh._row
        else:
            parts = []
        if not parts:
            vecs: np.ndarray = np.zeros((0, self.dim), np.float16)
        elif len(parts) == 1:
            vecs = parts[0]
        else:
            vecs = np.concatenate(parts)
        return Snapshot(vecs=vecs, key_row=dict(self._row))

    def _rewrite(self, keys: list[str], vecs: np.ndarray) -> None:
        self._manifest["segments"] = []
        self._keys, self._row = [], {}
        self.append(keys, vecs)
        _sweep(self.folder, set(self._manifest["segments"]))

    def merge(self) -> None:
        """Fold every segment into one, rows in the same order."""
        import numpy as np

        if len(self._manifest["segments"]) > 1:
            self._writing()
            snap = self.snapshot()
            self._rewrite(list(self._keys), np.asarray(snap.vecs))

    def collect(self, live: set[str]) -> int:
        """Rewrite the store without unused rows once they pass a quarter; rows dropped."""
        import numpy as np

        dead = sum(1 for k in self._keys if k not in live)
        if dead * 4 <= len(self._keys):
            return 0
        self._writing()
        snap = self.snapshot()
        keep = [k for k in self._keys if k in live]
        survivors = np.asarray(snap.vecs[[snap.key_row[k] for k in keep]])
        self._manifest["generation"] = self.generation + 1
        self._rewrite(keep, survivors)
        return dead


def _empty(header: dict[str, Any], dim: int) -> dict[str, Any]:
    return {"header": header, "dim": dim, "segments": [], "next": 0, "generation": 0}


def _read_keys(folder: Path, manifest: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for seg in manifest["segments"]:
        keys += json.loads((folder / f"{seg}.keys.json").read_text(encoding="utf-8"))
    return keys


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
    # The store's generation this table's vec_row numbers belong to.
    store_generation: int = 0

    def save(self, folder: Path) -> None:
        import numpy as np

        meta = json.dumps(
            {
                "collections": list(self.collections),
                "stamp": list(self.stamp),
                "complete": self.complete,
                "store_generation": self.store_generation,
            }
        )
        # Ids, keys and digests are hex: bytes, a quarter of numpy's four-byte str.
        arrays: dict[str, np.ndarray] = {
            "parent_id": self.parent_id.astype("S"),
            "chunk_seq": self.chunk_seq,
            "vec_row": self.vec_row,
            "member": self.member,
            "text_key": self.text_key.astype("S"),
            "meta": np.array([meta]),
        }
        if self.stored_digest is not None:
            arrays["stored_digest"] = self.stored_digest.astype("S")
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
                parent_id=z["parent_id"].astype(str),
                chunk_seq=z["chunk_seq"],
                vec_row=z["vec_row"],
                collections=tuple(meta["collections"]),
                member=z["member"],
                text_key=z["text_key"].astype(str),
                stamp=(str(meta["stamp"][0]), int(meta["stamp"][1]), int(meta["stamp"][2])),
                complete=bool(meta.get("complete", False)),
                stored_digest=z["stored_digest"].astype(str)
                if "stored_digest" in z.files
                else None,
                store_generation=int(meta.get("store_generation", 0)),
            )
