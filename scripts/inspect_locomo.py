"""Inspect LoCoMo raw data shape and convert to eimemory format."""
from __future__ import annotations

import json
import sys
from pathlib import Path

RAW = Path(sys.argv[1] if len(sys.argv) > 1 else r"E:\eimemory\data\locomo10.json")


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    print(f"type: {type(raw).__name__}, len: {len(raw)}")
    total_qa = 0
    for i, conv in enumerate(raw):
        qa = conv.get("qa", [])
        total_qa += len(qa)
        c = conv["conversation"]
        sessions = [k for k in c.keys() if k.startswith("session_") and not k.endswith("_date_time")]
        print(f"  conv[{i}] {c.get('speaker_a','?')} <-> {c.get('speaker_b','?')} | sessions={len(sessions)} | qa={len(qa)}")
    print(f"TOTAL qa: {total_qa}")
    c0 = raw[0]["conversation"]
    turn0 = c0["session_1"][0]
    print(f"\nSample turn keys: {list(turn0.keys())}")
    print(f"Sample turn: {turn0}")
    qa0 = raw[0]["qa"][0]
    print(f"\nSample qa keys: {list(qa0.keys())}")
    print(f"Sample qa: {qa0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
