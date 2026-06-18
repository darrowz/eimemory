"""Convert raw LoCoMo (snap-research/locomo) JSON into eimemory locomo adapter format.

Raw shape:
  list[conversation], each conv:
    conversation.speaker_a / speaker_b
    conversation.session_N_date_time
    conversation.session_N: list[{speaker, dia_id, text, ...}]
    qa: list[{question, answer, evidence: ["D1:3", ...], category}]

Eimemory shape (locomo.py uses longmemeval's _normalize_case):
  cases[i]:
    case_id, question, question_type
    haystack_sessions: [
      {session_id, turns: [{turn_id, messages: [{role, content}]}]}
    ]
    evidence_session_ids: [...]
    evidence_turn_ids: [...]

We emit one case per QA question; the same conversation may be referenced by
many cases, but the adapter's _existing_raw_chunk cache prevents re-ingestion.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def convert(raw_path: Path, out_path: Path) -> tuple[int, int]:
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    cases: list[dict] = []
    for conv_idx, conv in enumerate(raw):
        c = conv["conversation"]
        sess_keys = sorted([k for k in c.keys() if k.startswith("session_") and not k.endswith("_date_time")],
                           key=lambda k: int(k.split("_")[1]))
        haystack_sessions = []
        for sk in sess_keys:
            sess = c[sk]
            if not isinstance(sess, list):
                continue
            turns = []
            for t in sess:
                if not isinstance(t, dict) or "dia_id" not in t:
                    continue
                role = t.get("speaker") or t.get("role") or ""
                content = t.get("text") or t.get("content") or ""
                if not content:
                    continue
                turns.append({
                    "turn_id": t["dia_id"],
                    "messages": [{"role": role, "content": content}],
                })
            if not turns:
                continue
            sess_n = int(sk.split("_")[1])
            haystack_sessions.append({
                "session_id": f"conv{conv_idx}-s{sess_n}",
                "turns": turns,
            })
        if not haystack_sessions:
            continue

        for qa_idx, qa in enumerate(conv.get("qa") or []):
            q = qa.get("question") or ""
            if not q:
                continue
            evidence = list(qa.get("evidence") or [])
            evidence_session_ids = sorted({f"conv{conv_idx}-s{dia.split(':')[0][1:]}" for dia in evidence if dia.startswith("D") and ":" in dia})
            evidence_turn_ids = [dia for dia in evidence if dia]
            cat = qa.get("category")
            if isinstance(cat, int):
                qtype = f"cat{cat}"
            else:
                qtype = str(cat or "unknown")
            case = {
                "case_id": f"locomo-c{conv_idx}-q{qa_idx}",
                "question": q,
                "question_type": qtype,
                "expected_answer": str(qa.get("answer") or ""),
                "haystack_sessions": haystack_sessions,
                "evidence_session_ids": evidence_session_ids,
                "evidence_turn_ids": evidence_turn_ids,
                "scope": {
                    "agent_id": "hongtu",
                    "workspace_id": "embodied",
                    "user_id": "darrow",
                },
                "meta": {
                    "source_conversation": f"conv{conv_idx}",
                    "locomo_category": cat,
                    "locomo_speaker_a": c.get("speaker_a", ""),
                    "locomo_speaker_b": c.get("speaker_b", ""),
                },
            }
            cases.append(case)

    out = {
        "name": "locomo10-full",
        "schema_version": 1,
        "scope": {
            "agent_id": "hongtu",
            "workspace_id": "embodied",
            "user_id": "darrow",
        },
        "cases": cases,
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return len(raw), len(cases)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    nconv, ncases = convert(Path(args.inp), Path(args.out))
    print(f"OK converted {nconv} conversations -> {ncases} cases -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
