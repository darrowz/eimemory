"""Download LongMemEval S cleaned to local data/ then push to server."""
from __future__ import annotations

import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

socket.setdefaulttimeout(30)

URL = "https://hf-mirror.com/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "E:/eimemory/data/longmemeval_s_cleaned.json")


def download_with_progress(url: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url} -> {out}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        total = int(resp.headers.get("Content-Length", "0") or 0)
        chunk = 1024 * 1024
        written = 0
        with out.open("wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                written += len(buf)
                if total:
                    pct = written * 100 // total
                    sys.stdout.write(f"\r  {written/1024/1024:.1f}/{total/1024/1024:.1f} MB ({pct}%)")
                    sys.stdout.flush()
    elapsed = time.time() - t0
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"\nOK  {size_mb:.1f} MB in {elapsed:.1f}s")


def verify(path: Path) -> None:
    print(f"Verifying {path}")
    with path.open("rb") as f:
        head = f.read(8)
    if head[:1] != b"[":
        # may be NDJSON or something; print first 200 chars
        text = path.read_text("utf-8", errors="replace")[:300]
        print(f"WARN first 300 chars: {text!r}")
    else:
        # try to load as JSON array, count cases
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            print(f"OK JSON array, {len(data)} cases, first id: {data[0].get('question_id', data[0].get('id', '?'))}")
        else:
            print(f"WARN JSON object, keys: {list(data.keys())[:10]}")


if __name__ == "__main__":
    download_with_progress(URL, OUT)
    verify(OUT)
