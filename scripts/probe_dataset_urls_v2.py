"""Test which dataset URL works for downloading from this machine."""
from __future__ import annotations

import urllib.request
import socket

socket.setdefaulttimeout(20)

URLS = [
    # HF mirror (Chinese)
    "https://hf-mirror.com/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json",
    "https://hf-mirror.com/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/README.md",
    # Direct HF
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json",
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/README.md",
    # LoCoMo attempts
    "https://hf-mirror.com/datasets/snap-stanford/locomo/resolve/main/locomo10.json",
    "https://hf-mirror.com/datasets/snap-research/locomo/resolve/main/locomo10.json",
    "https://hf-mirror.com/datasets/locomo-bench/resolve/main/locomo10.json",
]

for u in URLS:
    try:
        req = urllib.request.Request(u, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            length = r.headers.get("Content-Length", "?")
            print(f"OK  {r.status}  len={length}  {u}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}  {u}")
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {e}  {u}")
