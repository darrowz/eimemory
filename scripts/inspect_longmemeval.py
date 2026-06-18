"""Inspect LongMemEval raw data shape."""
import json

d = json.load(open(r"E:\eimemory\data\longmemeval_s_cleaned.json", encoding="utf-8"))
print(f"type: {type(d).__name__}, len: {len(d)}")
sample = d[0]
print(f"sample keys: {list(sample.keys())}")
q = sample.get("question") or sample.get("query")
print(f"sample question: {q[:200] if q else None}")
a = sample.get("answer") or sample.get("expected_answer")
print(f"sample answer: {a}")
# Find haystack-like field
for k in sample.keys():
    v = sample[k]
    if isinstance(v, list) and v and isinstance(v[0], dict):
        print(f"  {k}: list[dict] len={len(v)}, first keys={list(v[0].keys())[:8]}")
        if "session_id" in v[0] or "turns" in v[0] or "messages" in v[0] or "haystack" in k.lower():
            print(f"    SAMPLE first item: {str(v[0])[:500]}")
            break
print(f"evidence_session_ids: {sample.get('evidence_session_ids')}")
print(f"evidence_turn_ids: {sample.get('evidence_turn_ids')}")
print(f"question_type: {sample.get('question_type')}")
print(f"session_count_in_haystack: {len([k for k in sample.keys() if k.startswith('haystack_')])}")
