"""Download LongMemEval S cleaned to local data/ then push to server."""
from __future__ import annotations

import codecs
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

socket.setdefaulttimeout(30)

URL = "https://hf-mirror.com/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "E:/eimemory/data/longmemeval_s_cleaned.json")
MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB download cap


def download_with_progress(url: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url} -> {out}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        total = int(resp.headers.get("Content-Length", "0") or 0)
        chunk = 1024 * 1024
        written = 0
        exceeded = False
        with out.open("wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                written += len(buf)
                if written > MAX_BYTES:
                    exceeded = True
                    break
                f.write(buf)
                if total:
                    pct = written * 100 // total
                    sys.stdout.write(f"\r  {written/1024/1024:.1f}/{total/1024/1024:.1f} MB ({pct}%)")
                    sys.stdout.flush()
    if exceeded:
        out.unlink(missing_ok=True)
        raise SystemExit(
            f"ERROR: download exceeded {MAX_BYTES // (1024 * 1024 * 1024)} GB limit "
            f"({written} bytes); partial file deleted."
        )
    elapsed = time.time() - t0
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"\nOK  {size_mb:.1f} MB in {elapsed:.1f}s")


def verify_integrity(path: Path) -> None:
    """Check a bounded UTF-8 prefix; verify() parses the complete JSON file."""
    with path.open("rb") as f:
        block = f.read(1024 * 1024)  # 1 MB
        truncated = bool(f.read(1))
    if not block:
        raise SystemExit("ERROR: downloaded file is empty")
    try:
        decoder = codecs.getincrementaldecoder("utf-8")()
        text = decoder.decode(block, final=not truncated)
    except UnicodeDecodeError as exc:
        raise SystemExit(f"ERROR: downloaded file is not valid UTF-8: {exc}")
    stripped = text.lstrip()
    if not stripped and truncated:
        return  # Leading whitespace may fill the prefix; verify() checks fully.
    if not stripped or stripped[0] not in ("[", "{"):
        raise SystemExit("ERROR: downloaded file does not start with valid JSON")


def verify(path: Path) -> None:
    print(f"Verifying {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            text = f.read()
        data = json.loads(text)
    except UnicodeDecodeError as exc:
        raise SystemExit(f"ERROR: downloaded file is not valid UTF-8: {exc}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: downloaded file is not valid JSON: {exc}")
    if isinstance(data, list):
        if not data:
            raise SystemExit("ERROR: downloaded JSON array is empty")
        if not isinstance(data[0], dict):
            raise SystemExit("ERROR: first downloaded JSON array record is not an object")
        print(f"OK JSON array, {len(data)} cases, first id: {data[0].get('question_id', data[0].get('id', '?'))}")
    elif isinstance(data, dict):
        print(f"WARN first 300 chars: {text[:300]!r}")
    else:
        raise SystemExit("ERROR: downloaded JSON must be an array or object")


if __name__ == "__main__":
    download_with_progress(URL, OUT)
    verify_integrity(OUT)
    verify(OUT)
