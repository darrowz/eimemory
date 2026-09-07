#!/usr/bin/env python3
"""Switch only the disabled, optional reranker to a verified derived artifact.

Retains the stopped original container and the private client configuration.
Does not enable production admission. A separate frozen acceptance is mandatory.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from provision_reranker import IMAGE
from quantize_reranker import REVISION, digest


def verify_artifact(path, *, root=Path("/var/lib/eimemory-reranker/artifacts")):
    root = Path(root).resolve(strict=True)
    path = Path(path)
    if path.is_symlink() or path.resolve(strict=True).parent != root:
        raise ValueError("artifact_path_invalid")
    raw = (path / "manifest.json").read_bytes()
    if sha256(raw).hexdigest() != path.name:
        raise ValueError("artifact_manifest_digest_mismatch")
    manifest = json.loads(raw)
    if (manifest.get("source_model") != "BAAI/bge-reranker-base"
            or manifest.get("source_revision") != REVISION
            or not {"model.onnx", "config.json", "tokenizer.json"} <= set(manifest["files"])):
        raise ValueError("artifact_provenance_invalid")
    for name, expected in manifest["files"].items():
        file = path / name
        if Path(name).name != name or file.is_symlink() or digest(file) != expected:
            raise ValueError("artifact_file_digest_mismatch")
    return path, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    artifact, manifest = verify_artifact(args.artifact)
    client = Path("/etc/eimemory/reranker.env")
    original = client.read_text()
    env = dict(line.split("=", 1) for line in original.splitlines() if line and not line.startswith("#"))
    if env.get("EIMEMORY_RERANKER_ENABLED") != "0":
        raise RuntimeError("production_admission_must_be_disabled")
    old = "eimemory-reranker-fp32-" + REVISION[:12]
    state = json.loads(subprocess.check_output(["docker", "inspect", "--format", "{{json .State}}", "eimemory-reranker"]))
    if state.get("Running"):
        raise RuntimeError("original_reranker_must_be_stopped")
    backup = client.with_name("reranker.env.pre-int8")
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(original)
    subprocess.run(["docker", "rename", "eimemory-reranker", old], check=True)
    model_id = "/model/" + artifact.name
    command = ["docker", "run", "-d", "--name", "eimemory-reranker", "--restart", "no",
        "--cpus", "1", "--memory", "2g", "--memory-swap", "2g", "--pids-limit", "128",
        "--user", f"{os.getuid()}:{os.getgid()}", "--security-opt", "no-new-privileges:true",
        "--cap-drop", "ALL", "--log-driver", "none", "--env-file", "/etc/eimemory/reranker-service.env",
        "-p", "127.0.0.1:8089:80", "-v", str(artifact) + ":" + model_id + ":ro",
        IMAGE, "--model-id", model_id, "--revision", REVISION, "--port", "80",
        "--tokenization-workers", "1", "--max-concurrent-requests", "32",
        "--max-batch-tokens", "512", "--max-batch-requests", "1", "--max-client-batch-size", "32"]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise RuntimeError("derived_container_start_failed_original_retained")
    ready = False
    for _ in range(60):
        try:
            req = urllib.request.Request("http://127.0.0.1:8089/info",
                headers={"Authorization": "Bearer " + env["EIMEMORY_RERANKER_API_KEY"]})
            with urllib.request.urlopen(req, timeout=2) as response:
                info = json.load(response)
            ready = (info.get("model_id") == model_id and info.get("model_sha") == REVISION
                     and "reranker" in (info.get("model_type") or {}))
            if ready:
                break
        except Exception:
            pass
        time.sleep(1)
    if not ready:
        subprocess.run(["docker", "stop", "eimemory-reranker"], capture_output=True)
        raise RuntimeError("derived_reranker_not_ready_disabled_original_retained")
    env["EIMEMORY_RERANKER_MODEL"] = model_id
    env["EIMEMORY_RERANKER_CALIBRATION"] = "unvalidated-int8-" + artifact.name
    staged = client.with_name("reranker.env.int8-staged")
    fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in env.items()))
    os.replace(staged, client)
    print(json.dumps({"healthy": True, "production_enabled": False, "artifact": artifact.name,
                      "model_id": model_id, "original_container": old, "memory_limit_mib": 2048}))


if __name__ == "__main__":
    main()
