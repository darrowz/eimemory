"""Convert raw LongMemEval (xiaowu0162/longmemeval-cleaned) to eimemory longmemeval format.

Raw shape (cleaned variant):
  haystack_sessions:   list[list[{role, content, has_answer}]]  (53 sessions, each a list of messages)
  haystack_session_ids: list[str]                               (parallel to haystack_sessions)
  haystack_dates:       list[str]                               (parallel)
  question, answer, answer_session_ids, question_type, question_id

Eimemory shape (locomo/longmemeval adapter):
  haystack_sessions: list[{session_id, turns: [{turn_id, messages: [{role, content}]}]}]
  evidence_session_ids, evidence_turn_ids, evidence_chunk_ids

We treat each raw session as ONE turn per message (so turn_id = message index within session),
which preserves fine-grained evidence alignment if the dataset ever provides it.

Evidence mining
---------------

The cleaned variant does **not** publish a top-level ``evidence_*`` field.
Real evidence lives on individual messages as the boolean ``has_answer`` —
the official LongMemEval signal that the message contributed to the
reference answer. We mine it as follows:

* ``evidence_session_ids`` ← union of
    - ``answer_session_ids`` (raw, includes abstract ``*_abs`` session IDs)
    - any session that contains at least one ``has_answer=True`` message
* ``evidence_turn_ids`` ← ``f"{session_id}:m{msg_idx}"`` for every
  message where ``has_answer`` is truthy. This matches the ``turn_id``
  format we emit in the eimemory haystack, so the LME adapter's
  ``_expected_ids(turn)`` set-membership check works end-to-end.
* The official LongMemEval schema's top-level ``evidence_session_ids`` /
  ``evidence_turn_ids`` (when present, e.g. older raw variants) are also
  unioned in for forward compatibility.

Set ``_USE_REAL_EVIDENCE = False`` to disable mining and reproduce the
historical hard-coded behaviour (empty turn IDs, session IDs from
``answer_session_ids`` only).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Feature flag. Default ON — the historical hard-coded ``evidence_turn_ids = []``
# is what masked retrieval gaps in earlier evals and is being phased out.
_USE_REAL_EVIDENCE = True


def _extract_real_evidence(
    raw_case: dict,
    *,
    haystack_session_ids: list[str] | None = None,
    haystack_sessions: list | None = None,
) -> tuple[list[str], list[str]]:
    """Pull real evidence fields from a raw LongMemEval case.

    Returns ``(session_ids, turn_ids)``.

    ``session_ids`` is the union of:

    * ``answer_session_ids`` (raw, may include abstract ``*_abs`` IDs)
    * ``evidence_session_ids`` (top-level, when present on older variants)
    * any session that contains at least one ``has_answer=True`` message

    ``turn_ids`` is the list of ``f"{session_id}:m{msg_idx}"`` markers
    for every message where ``has_answer`` is truthy, merged with the
    top-level ``evidence_turn_ids`` field when present. The string
    format matches the ``turn_id`` we emit on the eimemory side, so the
    adapter's set-membership check is a direct match.
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

    # Mine the per-message ``has_answer`` boolean — the actual official
    # LongMemEval evidence signal. We need both the haystack and the
    # parallel session-id list to translate (sess_idx, msg_idx) into
    # the ``{sid}:m{msg_idx}`` turn-id format the adapter expects.
    if haystack_sessions is not None and haystack_session_ids is not None:
        for sess_idx, sess_msgs in enumerate(haystack_sessions):
            if not isinstance(sess_msgs, list):
                continue
            sid = (
                haystack_session_ids[sess_idx]
                if sess_idx < len(haystack_session_ids)
                else f"s{sess_idx}"
            )
            sid_str = str(sid or "").strip()
            if not sid_str:
                continue
            has_answer_in_session = False
            for msg_idx, message in enumerate(sess_msgs):
                if not isinstance(message, dict):
                    continue
                if not message.get("has_answer"):
                    continue
                turn_id = f"{sid_str}:m{msg_idx}"
                if turn_id not in turn_ids:
                    turn_ids.append(turn_id)
                has_answer_in_session = True
            if has_answer_in_session and sid_str not in session_ids:
                session_ids.append(sid_str)

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
            evidence_session_ids, evidence_turn_ids = _extract_real_evidence(
                c,
                haystack_session_ids=hs_ids,
                haystack_sessions=hs,
            )
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
                "evidence_turn_id_count": len(evidence_turn_ids),
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
