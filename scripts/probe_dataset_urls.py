"""Probe known LongMemEval and LoCoMo dataset URLs."""
from __future__ import annotations

import socket
import urllib.request

socket.setdefaulttimeout(15)

URLS = [
    # Hugging Face - LongMemEval (cleaned)
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s.json",
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json",
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval.json",
    # LoCoMo (Snap Research) - various paths
    "https://raw.githubusercontent.com/snap-stanford/locomo/main/data/locomo10.json",
    "https://raw.githubusercontent.com/snap-lstm/locomo-bench/main/data/locomo10.json",
    "https://huggingface.co/datasets/snap-stanford/locomo/resolve/main/locomo10.json",
    "https://huggingface.co/datasets/Anthropic/locomo-bench/resolve/main/locomo10.json",
    "https://raw.githubusercontent.com/Locomo-Bench/locomo-bench/main/data/locomo10.json",
]


def head(url: str) -> None:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            length = r.headers.get("Content-Length", "?")
            print(f"OK  {r.status}  size={length}  {url}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}  {url}")
    except Exception as e:
        print(f"FAIL {type(e).__name__}  {url}")


for u in URLS:
    head(u)
