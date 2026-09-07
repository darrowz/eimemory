#!/usr/bin/env python3
"""Provision the optional honxin reranker without enabling RPC admission.

Run explicitly on the deployment host. Never prints credentials or container
startup logs (TEI can log its parsed API key). Existing resources are not replaced.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import urllib.request

IMAGE = "ghcr.io/huggingface/text-embeddings-inference@sha256:ad950d30878eceb72aaf32024d26fa2b1d04a75304fa0b4776b49aa1941fea07"
MODEL = "BAAI/bge-reranker-base"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="")
    args = parser.parse_args()
    revision = args.revision
    if not revision:
        with urllib.request.urlopen("https://huggingface.co/api/models/" + MODEL, timeout=30) as response:
            revision = json.load(response)["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("model_revision_invalid")
    server_env = Path("/etc/eimemory/reranker-service.env")
    client_env = Path("/etc/eimemory/reranker.env")
    cache = Path("/var/lib/eimemory-reranker")
    existing = subprocess.run(["docker", "container", "inspect", "eimemory-reranker"], capture_output=True)
    if server_env.exists() or client_env.exists() or existing.returncode == 0:
        raise RuntimeError("reranker_resources_already_exist_inspect_before_change")
    subprocess.run(["sudo", "-n", "install", "-d", "-m", "0750", "-o", str(os.getuid()),
                    "-g", str(os.getgid()), str(cache)], check=True)
    key = secrets.token_urlsafe(32)
    server_text = f"API_KEY={key}\nOMP_NUM_THREADS=1\nMKL_NUM_THREADS=1\nRAYON_NUM_THREADS=1\n"
    client_text = (f"EIMEMORY_RERANKER_ENABLED=0\nEIMEMORY_RERANKER_URL=http://127.0.0.1:8089\n"
        f"EIMEMORY_RERANKER_API_KEY={key}\nEIMEMORY_RERANKER_MODEL={MODEL}\n"
        f"EIMEMORY_RERANKER_REVISION={revision}\nEIMEMORY_RERANKER_MAX_CANDIDATES=12\n"
        "EIMEMORY_RERANKER_TEXT_CHARS=700\nEIMEMORY_RERANKER_TIMEOUT_SECONDS=2\n"
        "EIMEMORY_RERANKER_MIN_SCORE=0\nEIMEMORY_RERANKER_CALIBRATION=unvalidated\n")
    for path, content in ((server_env, server_text), (client_env, client_text)):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
    result = subprocess.run(["docker", "run", "-d", "--name", "eimemory-reranker",
        "--restart", "unless-stopped", "--cpus", "1", "--memory", "2g", "--memory-swap", "2g",
        "--pids-limit", "128", "--user", f"{os.getuid()}:{os.getgid()}",
        "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL", "--log-driver", "none",
        "--env-file", str(server_env), "-p", "127.0.0.1:8089:80", "-v", str(cache) + ":/data",
        IMAGE, "--model-id", MODEL, "--revision", revision, "--port", "80",
        # TEI acquires one permit per candidate, before batching inference.
        "--tokenization-workers", "1", "--max-concurrent-requests", "32",
        "--max-batch-tokens", "512", "--max-batch-requests", "1", "--max-client-batch-size", "32"],
        capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("reranker_container_start_failed_configuration_retained")
    print(json.dumps({"container": "eimemory-reranker", "model": MODEL, "revision": revision,
        "image": IMAGE, "cpus": 1, "memory_limit_mib": 2048, "rpc_enabled": False,
        "listen": "127.0.0.1:8089", "logging": "disabled_to_prevent_credential_disclosure"}))


if __name__ == "__main__":
    main()
