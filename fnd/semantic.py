"""The embedding model: what it is, where it lives, and how sections become vectors.

A model is data (:class:`ModelSpec`), fetched at a pinned revision and verified file by
file. numpy, onnxruntime and tokenizers load only inside :func:`load_embedder`, so fnd
imports this module whether or not the semantic extra is installed.
"""

from __future__ import annotations

import hashlib
import platform
import sys
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from fnd import paths

if TYPE_CHECKING:
    import numpy as np

__all__ = [
    "GRANITE",
    "MODELS",
    "Embedder",
    "ModelFetchError",
    "ModelFile",
    "ModelSpec",
    "fetch_model",
    "file_url",
    "is_model_present",
    "is_ready",
    "load_embedder",
    "model_dir",
    "models_dir",
    "platform_supported",
    "section_key",
    "section_text",
]


class ModelFetchError(RuntimeError):
    """A model file could not be downloaded or did not match its checksum."""


@dataclass(frozen=True, slots=True)
class ModelFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    repo: str
    source: str
    revision: str
    files: tuple[ModelFile, ...]
    max_tokens: int
    dim: int


GRANITE = ModelSpec(
    name="granite-r2-q8",
    repo="ibm-granite/granite-embedding-small-english-r2",
    source="onnx-community/granite-embedding-small-english-r2-ONNX",
    revision="1dc7835ba0cb9c76a3618d0bf0c427c97671b3c8",
    files=(
        ModelFile(
            "onnx/model_quantized.onnx",
            "a3fad524afc3f060216a8ddbb1ac89c9b6498fba8995b5718bde879076a2e9ba",
            598_902,
        ),
        ModelFile(
            "onnx/model_quantized.onnx_data",
            "1f4cf47e4adec7f7ae09db03d071ba8667e07f9a4203142c7efa8d37fe453597",
            51_885_568,
        ),
        ModelFile(
            "tokenizer.json",
            "feeb83348dcb033bc6b9d2e1f7906ca9eb2d122845000c9416d894d7c2927149",
            2_128_614,
        ),
    ),
    max_tokens=256,
    dim=384,
)
MODELS: dict[str, ModelSpec] = {GRANITE.name: GRANITE}


def models_dir() -> Path:
    return paths.app_cache_dir() / "models"


def model_dir(spec: ModelSpec) -> Path:
    return models_dir() / f"{spec.name}-{spec.revision[:12]}"


def file_url(spec: ModelSpec, f: ModelFile) -> str:
    return f"https://huggingface.co/{spec.source}/resolve/{spec.revision}/{f.path}"


def _local(spec: ModelSpec, f: ModelFile) -> Path:
    return model_dir(spec) / Path(f.path).name


def is_model_present(spec: ModelSpec = GRANITE) -> bool:
    return all(
        _local(spec, f).is_file() and _local(spec, f).stat().st_size == f.size for f in spec.files
    )


def fetch_model(
    spec: ModelSpec = GRANITE,
    *,
    opener: Callable[[str], Any] = urllib.request.urlopen,
    on_bytes: Callable[[int, int], None] | None = None,
) -> Path:
    """Download each missing file at the pinned revision, verified before it is kept."""
    total = sum(f.size for f in spec.files)
    done = 0
    for f in spec.files:
        dest = _local(spec, f)
        if dest.is_file() and dest.stat().st_size == f.size:
            done += f.size
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        try:
            with opener(file_url(spec, f)) as src, part.open("wb") as out:
                while chunk := src.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
                    done += len(chunk)
                    if on_bytes is not None:
                        on_bytes(done, total)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise ModelFetchError(f"could not download {f.path}: {e}") from e
        if digest.hexdigest() != f.sha256:
            part.unlink(missing_ok=True)
            raise ModelFetchError(f"{f.path} did not match its recorded checksum")
        part.replace(dest)
    return model_dir(spec)


def platform_supported(
    system: str | None = None, machine: str | None = None, mac_ver: str | None = None
) -> bool:
    """onnxruntime ships macOS wheels for arm64 on macOS 14 and later only."""
    system = platform.system() if system is None else system
    if system != "Darwin":
        return True
    machine = (platform.machine() if machine is None else machine).lower()
    mac_ver = platform.mac_ver()[0] if mac_ver is None else mac_ver
    major = int(mac_ver.split(".")[0]) if mac_ver else 0
    return machine == "arm64" and major >= 14


def is_ready(spec: ModelSpec = GRANITE) -> bool:
    """Installed here: a supported platform, the runtime importable, the model on disk."""
    import importlib
    import importlib.util

    importlib.invalidate_caches()  # an in-app install lands after this process started
    return (
        platform_supported()
        and importlib.util.find_spec("onnxruntime") is not None
        and importlib.util.find_spec("tokenizers") is not None
        and is_model_present(spec)
    )


def section_text(title: str, heading_path: str, body: str) -> str:
    head = heading_path if heading_path != title else ""
    return " ".join(x for x in (title, head, body) if x)


def section_key(token_ids: Sequence[int], max_tokens: int) -> str:
    """What a vector is filed under: the tokens the model reads, so later text costs nothing."""
    import struct

    ids = list(token_ids)[:max_tokens]
    return hashlib.sha1(struct.pack(f"<{len(ids)}i", *ids), usedforsecurity=False).hexdigest()


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...

    def token_ids(self, text: str) -> list[int]: ...


class OnnxEmbedder:
    """CLS-pooled, unit-length vectors from an ONNX transformer."""

    def __init__(self, spec: ModelSpec, *, threads: int) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self._sess = ort.InferenceSession(
            str(_local(spec, spec.files[0])), opts, providers=["CPUExecutionProvider"]
        )
        self._tok = Tokenizer.from_file(str(_local(spec, spec.files[-1])))
        self._tok.enable_truncation(spec.max_tokens)
        self._inputs = {i.name for i in self._sess.get_inputs()}
        self.dim = spec.dim

    def token_ids(self, text: str) -> list[int]:
        return list(self._tok.encode(text).ids)

    def embed(self, texts: list[str]) -> np.ndarray:
        """One text per run: the int8 model scales each run by its whole input, so a
        batched vector moved with its batch-mates (cosine 0.994). Unbatched is also
        faster here: 84 sections a second against 57 at 16 (M1 Max, no padding)."""
        np = self._np
        out = np.empty((len(texts), self.dim), dtype=np.float32)
        for i, enc in enumerate(self._tok.encode_batch(texts)):
            ids = np.array([enc.ids], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            pooled = np.asarray(self._sess.run(None, feed)[0])[0, 0, :]
            out[i] = pooled / max(float(np.linalg.norm(pooled)), 1e-12)
        return out


def load_embedder(spec: ModelSpec = GRANITE, *, threads: int) -> Embedder:
    return OnnxEmbedder(spec, threads=threads)


def _main(argv: list[str]) -> int:
    from fnd.cli_output import echo

    if argv[:1] != ["fetch"]:
        echo("usage: python -m fnd.semantic fetch", err=True)
        return 2
    try:
        fetch_model(on_bytes=lambda d, t: echo(f"downloaded {d * 100 // t}%"))
    except ModelFetchError as e:
        echo(str(e), err=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
