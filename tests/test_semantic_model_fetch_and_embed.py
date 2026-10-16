"""The model is fetched, verified and loaded only as recorded."""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys
from pathlib import Path

import pytest

from fnd import semantic
from fnd.semantic import ModelFetchError, ModelFile, ModelSpec


def _spec(payload: bytes) -> ModelSpec:
    return ModelSpec(
        name="tiny",
        repo="x/tiny",
        source="x/tiny",
        revision="abc",
        files=(ModelFile("tokenizer.json", hashlib.sha256(payload).hexdigest(), len(payload)),),
        max_tokens=8,
        dim=4,
    )


@pytest.fixture
def models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(semantic, "models_dir", lambda: tmp_path)
    return tmp_path


def test_a_verified_file_is_kept(models: Path) -> None:
    """A file whose checksum matches is renamed into place."""
    spec = _spec(b"ok")
    semantic.fetch_model(spec, opener=lambda url: io.BytesIO(b"ok"))
    assert semantic.is_model_present(spec)


def test_a_wrong_checksum_leaves_nothing(models: Path) -> None:
    """A mismatching download raises and leaves no file or part behind."""
    spec = _spec(b"ok")
    with pytest.raises(ModelFetchError):
        semantic.fetch_model(spec, opener=lambda url: io.BytesIO(b"tampered"))
    assert not any(p.is_file() for p in models.rglob("*"))


def test_a_present_file_is_not_downloaded_again(models: Path) -> None:
    """A second fetch opens nothing when every file is already in place."""
    spec = _spec(b"ok")
    semantic.fetch_model(spec, opener=lambda url: io.BytesIO(b"ok"))

    def refuse(url: str) -> io.BytesIO:
        raise AssertionError(url)

    semantic.fetch_model(spec, opener=refuse)


def test_the_url_pins_the_revision() -> None:
    """Files are fetched from the recorded revision, never main."""
    url = semantic.file_url(semantic.GRANITE, semantic.GRANITE.files[0])
    assert "/resolve/1dc7835ba0cb9c76a3618d0bf0c427c97671b3c8/" in url


def test_a_missing_model_file_reads_as_not_installed(models: Path) -> None:
    """A model folder with a file removed by hand is not present."""
    spec = _spec(b"ok")
    semantic.fetch_model(spec, opener=lambda url: io.BytesIO(b"ok"))
    (semantic.model_dir(spec) / "tokenizer.json").unlink()
    assert not semantic.is_model_present(spec)


@pytest.mark.parametrize(
    ("system", "machine", "mac_ver", "ok"),
    [
        ("Darwin", "arm64", "14.5", True),
        ("Darwin", "arm64", "13.6", False),
        ("Darwin", "x86_64", "14.5", False),
        ("Linux", "x86_64", "", True),
        ("Linux", "aarch64", "", True),
        ("Windows", "AMD64", "", True),
        ("Windows", "ARM64", "", True),
    ],
)
def test_platform_support(system: str, machine: str, mac_ver: str, ok: bool) -> None:
    """Install is offered only where onnxruntime ships a wheel."""
    assert semantic.platform_supported(system, machine, mac_ver) is ok


def test_section_text_matches_the_measured_recipe() -> None:
    """Title, a differing heading path, then body."""
    assert semantic.section_text("Wk4", "Wk4 > Deep", "body") == "Wk4 Wk4 > Deep body"
    assert semantic.section_text("Wk4", "Wk4", "body") == "Wk4 body"


def test_importing_needs_no_optional_packages() -> None:
    """`fnd.semantic` imports without loading numpy, onnxruntime or tokenizers."""
    code = (
        "import fnd.semantic, sys; "
        "assert not {'numpy', 'onnxruntime', 'tokenizers'} & set(sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


@pytest.fixture
def real_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real model, from FND_SEMANTIC_MODELS_DIR (a models folder holding it)."""
    import os

    folder = os.environ.get("FND_SEMANTIC_MODELS_DIR")
    if not folder:
        pytest.skip("set FND_SEMANTIC_MODELS_DIR to run against the real model")
    monkeypatch.setattr(semantic, "models_dir", lambda: Path(folder))
    if not semantic.is_model_present(semantic.GRANITE):
        pytest.skip("no model in FND_SEMANTIC_MODELS_DIR")


@pytest.mark.usefixtures("real_model")
def test_granite_embeds_a_paraphrase_near_its_text() -> None:
    """The real model puts a paraphrase closer to its passage than to an unrelated one."""
    e = semantic.load_embedder(threads=1)
    q, near, far = e.embed(
        [
            "how plants make food from light",
            "Photosynthesis converts sunlight into chemical energy.",
            "The invoice is due on Friday.",
        ]
    )
    assert float(q @ near) > float(q @ far)


@pytest.mark.usefixtures("real_model")
def test_a_vector_does_not_depend_on_its_batch_mates() -> None:
    """The same text gives the same vector alone or among others."""
    import numpy as np

    e = semantic.load_embedder(threads=1)
    text = "Photosynthesis converts sunlight into chemical energy."
    alone = e.embed([text])[0]
    among = e.embed(["The invoice is due on Friday.", text, "A short note."])[1]
    assert np.allclose(alone, among, atol=1e-6)
