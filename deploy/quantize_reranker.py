#!/usr/bin/env python3
"""Package the pinned reranker with official ORT dynamic INT8 quantization.

Run in an isolated build venv under a resource-limited service. This never
changes a serving model, the Hugging Face cache, or any memory database.
The derived model path is content-addressed and must be mounted read-only.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"
ORT_VERSION = "1.29.0"


def digest(path):
    with path.open("rb") as stream:
        return sha256_file(stream)


def sha256_file(stream):
    result = sha256()
    while block := stream.read(1024 * 1024):
        result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("/var/lib/eimemory-reranker"))
    args = parser.parse_args()
    import onnxruntime
    from onnxruntime.quantization import QuantType, quantize_dynamic
    if onnxruntime.__version__ != ORT_VERSION:
        raise RuntimeError("quantizer_version_mismatch")
    cache = args.cache.resolve(strict=True)
    source = cache / "models--BAAI--bge-reranker-base" / "snapshots" / REVISION
    source_model = source / "onnx" / "model.onnx"
    if not source_model.is_file():
        raise RuntimeError("pinned_source_model_missing")
    artifacts = cache / "artifacts"
    artifacts.mkdir(exist_ok=True, mode=0o750)
    if artifacts.is_symlink():
        raise RuntimeError("artifact_directory_symlink")
    # Keep an incomplete build separate; never overwrite a verified artifact.
    work = Path(tempfile.mkdtemp(prefix="building-", dir=artifacts))
    for name in ("config.json", "tokenizer.json", "tokenizer_config.json",
                 "special_tokens_map.json", "sentencepiece.bpe.model"):
        if (source / name).is_file():
            shutil.copyfile(source / name, work / name)
    quantize_dynamic(str(source_model), str(work / "model.onnx"),
                     op_types_to_quantize=["MatMul", "Gather"],
                     per_channel=True, weight_type=QuantType.QInt8,
                     extra_options={"MatMulConstBOnly": True})
    files = {p.name: digest(p) for p in sorted(work.iterdir()) if p.is_file()}
    manifest = {"schema": "reranker_artifact.v1", "source_model": "BAAI/bge-reranker-base",
                "source_revision": REVISION, "source_onnx_sha256": digest(source_model),
                "onnxruntime": ORT_VERSION, "quantization": "dynamic-int8-matmul-gather-per-channel",
                "files": files}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    identity = sha256(raw).hexdigest()
    (work / "manifest.json").write_bytes(raw)
    for path in work.iterdir():
        path.chmod(0o440)
    work.chmod(0o550)
    target = artifacts / identity
    if target.exists():
        raise RuntimeError("artifact_already_exists_build_retained")
    work.rename(target)
    print(json.dumps({"artifact": str(target), "digest": identity,
                      "source_revision": REVISION, "model_bytes": (target / "model.onnx").stat().st_size}), flush=True)


if __name__ == "__main__":
    main()
