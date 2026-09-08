#!/usr/bin/env python3
"""Fetch pinned public CPU reranker artifacts; never activates production."""
from hashlib import sha256
import json
from pathlib import Path
import tarfile
import urllib.request

BUILD = "b10809"
MODEL_REPO = "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF"
MODEL_REVISION = "a02f48bb4f057028298c21fa033da2b30d7742d5"
MODEL_DIGEST = "22c9979ce4fbcdc5acdc310c6641c32797eff1aa980b8f7a2db8a8ea23429a48"
ARCHIVE_DIGEST = "5e34434ddc6d03cd1584f403201aff0d4bd1a5793a72ff7e286532dfd1e4b941"


def download(url, target, expected, size):
    if target.exists():
        with target.open("rb") as stream:
            result = sha256()
            while block := stream.read(1024 * 1024):
                result.update(block)
        if result.hexdigest() != expected:
            raise RuntimeError("existing_artifact_digest_mismatch")
        return
    temporary = target.with_suffix(target.suffix + ".partial")
    digest, total = sha256(), 0
    with urllib.request.urlopen(url, timeout=60) as response, temporary.open("xb") as stream:
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > size:
                raise RuntimeError("artifact_size_exceeded")
            stream.write(block)
            digest.update(block)
    if total != size or digest.hexdigest() != expected:
        raise RuntimeError("download_artifact_mismatch")
    temporary.rename(target)
    target.chmod(0o440)


def main():
    root = Path("/var/lib/eimemory-reranker/qwen3")
    root.mkdir(mode=0o750, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError("artifact_root_symlink")
    archive = root / (BUILD + ".tar.gz")
    download(f"https://github.com/ggml-org/llama.cpp/releases/download/{BUILD}/llama-{BUILD}-bin-ubuntu-x64.tar.gz",
             archive, ARCHIVE_DIGEST, 16734586)
    model = root / (MODEL_DIGEST + ".gguf")
    download(f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/qwen3-reranker-0.6b-q8_0.gguf",
             model, MODEL_DIGEST, 639153184)
    binaries = root / BUILD
    if not binaries.exists():
        binaries.mkdir(mode=0o750)
        with tarfile.open(archive) as tar:
            tar.extractall(binaries, filter="data")
    servers = list(binaries.rglob("llama-server"))
    if len(servers) != 1 or not servers[0].is_file():
        raise RuntimeError("server_binary_missing")
    print(json.dumps({"server": str(servers[0]), "model": str(model),
        "model_repo": MODEL_REPO, "model_revision": MODEL_REVISION,
        "model_digest": MODEL_DIGEST, "build": BUILD, "archive_digest": ARCHIVE_DIGEST}), flush=True)


if __name__ == "__main__":
    main()
