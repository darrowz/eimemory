"""Convert raw LongMemEval (xiaowu0162/longmemeval-cleaned) to eimemory longmemeval format.

Raw shape (cleaned variant):
  haystack_sessions:   list[list[{role, content}]]  (53 sessions, each a list of messages)
  haystack_session_ids: list[str]                   (parallel to haystack_sessions)
  haystack_dates:       list[str]                   (parallel)
  question, answer, answer_session_ids, question_type, question_id

Eimemory shape (locomo/longmemeval adapter):
  haystack_sessions: list[{session_id, turns: [{turn_id, messages: [{role, content}]}]}]
  evidence_session_ids, evidence_turn_ids, evidence_chunk_ids

We treat each raw session as ONE turn per message (so turn_id = message index within session),
which preserves fine-grained evidence alignment if the dataset ever provides it.

Evidence mining
---------------

The xiaowu0162/longmemeval-cleaned variant only carries ``answer_session_ids``
(real evidence at session granularity) and never carries ``evidence_turn_ids``.
Other LongMemEval variants may carry both. The converter therefore reads:

* ``answer_session_ids`` (preferred) or ``evidence_session_ids`` → ``evidence_session_ids``
* ``evidence_turn_ids`` (if present) → ``evidence_turn_ids``

Set ``_USE_REAL_EVIDENCE = False`` to disable mining and reproduce the
historical hard-coded behaviour (empty lists).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Feature flag. Default ON — the historical hard-coded ``evidence_turn_ids = []``
# is what masked retrieval gaps in earlier evals and is being phased out.
_USE_REAL_EVIDENCE = True


def _extract_real_evidence(raw_case: dict) -> tuple[list[str], list[str]]:
    """Pull real evidence fields from a raw LongMemEval case.

    Returns ``(session_ids, turn_ids)``. ``session_ids`` prefers
    ``answer_session_ids`` (the only evidence field populated by the
    xiaowu0162 cleaned variant) and falls back to ``evidence_session_ids``
    for completeness against the official LongMemEval schema. ``turn_ids``
    is taken from ``evidence_turn_ids`` when present; the cleaned variant
    never populates this field, so an empty list is the expected default.
    """
    session_ids: list[str] = []
    for field in ("answer_session_ids", "evidence_session_ids"):
        for value in list(raw_case.get(field) or []):
            text = str(value or "").strip()
            if text and text not in session_ids:
                session_ids.append(text)
    turn_ids: list[str] = []
    for value in list(raw_case.get("evidence_turn_ids") or []):
        text = str(value or "").strip()
        if text and text not in turn_ids:
            turn_ids.append(text)
    return session_ids, turn_ids


def convert(raw_path: Path, out_path: Path) -> int:
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    cases: list[dict] = []
    for i, c in enumerate(raw):
        hs = c.get("haystack_sessions")
        hs_ids = c.get("haystack_session_ids") or []
        hs_dates = c.get("haystack_dates") or []
        if not isinstance(hs, list) or not hs:
            continue
        eim_sessions: list[dict] = []
        for sess_idx, sess_msgs in enumerate(hs):
            if not isinstance(sess_msgs, list):
                continue
            sid = hs_ids[sess_idx] if sess_idx < len(hs_ids) else f"s{sess_idx}"
            sdate = hs_dates[sess_idx] if sess_idx < len(hs_dates) else ""
            turns: list[dict] = []
            for msg_idx, m in enumerate(sess_msgs):
                if not isinstance(m, dict):
                    continue
                role = m.get("role") or m.get("speaker") or ""
                content = m.get("content") or m.get("text") or m.get("message") or ""
                if not content:
                    continue
                turns.append({
                    "turn_id": f"{sid}:m{msg_idx}",
                    "messages": [{"role": role, "content": content}],
                })
            if not turns:
                continue
            eim_sessions.append({
                "session_id": str(sid),
                "session_date": str(sdate),
                "turns": turns,
            })
        if not eim_sessions:
            continue

        if _USE_REAL_EVIDENCE:
            evidence_session_ids, evidence_turn_ids = _extract_real_evidence(c)
        else:
            # Historical behaviour: only the session-level answer evidence
            # is wired in; turn-level evidence is hard-coded empty.
            evidence_session_ids = [str(s) for s in c.get("answer_session_ids") or []]
            evidence_turn_ids = []
        case = {
            "case_id": str(c.get("question_id") or f"lme-case-{i}"),
            "question": str(c.get("question") or ""),
            "question_type": str(c.get("question_type") or "unknown"),
            "expected_answer": str(c.get("answer") or ""),
            "question_date": str(c.get("question_date") or ""),
            "haystack_sessions": eim_sessions,
            "evidence_session_ids": evidence_session_ids,
            "evidence_turn_ids": evidence_turn_ids,
            "evidence_chunk_ids": [],
            "scope": {
                "agent_id": "hongtu",
                "workspace_id": "embodied",
                "user_id": "darrow",
            },
            "meta": {
                "source": "xiaowu0162/longmemeval-cleaned",
                "haystack_session_ids": [str(s) for s in hs_ids],
                "evidence_mining_enabled": _USE_REAL_EVIDENCE,
            },
        }
        cases.append(case)

    out = {
        "name": "longmemeval-s-cleaned",
        "schema_version": 1,
        "scope": {
            "agent_id": "hongtu",
            "workspace_id": "embodied",
            "user_id": "darrow",
        },
        "cases": cases,
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return len(cases)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    n = convert(Path(args.inp), Path(args.out))
    print(f"OK converted {n} cases -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
