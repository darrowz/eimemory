"""Derived deployment identity must bind bytes, not just the upstream revision."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def test_derived_artifact_rejects_changed_weights_and_wrong_directory(tmp_path, monkeypatch):
    deploy = Path(__file__).parents[1] / "deploy"
    monkeypatch.syspath_prepend(str(deploy))
    spec = importlib.util.spec_from_file_location("artifact_activation_test", deploy / "activate_reranker_artifact.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contents = {"model.onnx": b"weights", "config.json": b"{}", "tokenizer.json": b"{}"}
    manifest = {"source_model": "BAAI/bge-reranker-base", "source_revision": module.REVISION,
                "files": {name: sha256(data).hexdigest() for name, data in contents.items()}}
    raw = json.dumps(manifest, sort_keys=True).encode()
    artifact = tmp_path / sha256(raw).hexdigest()
    artifact.mkdir()
    (artifact / "manifest.json").write_bytes(raw)
    for name, data in contents.items():
        (artifact / name).write_bytes(data)
    assert module.verify_artifact(artifact, root=tmp_path)[1] == manifest
    with pytest.raises(ValueError, match="artifact_path_invalid"):
        module.verify_artifact(artifact, root=artifact)
    (artifact / "model.onnx").write_bytes(b"different weights")
    with pytest.raises(ValueError, match="artifact_file_digest_mismatch"):
        module.verify_artifact(artifact, root=tmp_path)
