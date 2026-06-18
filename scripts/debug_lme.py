"""Debug LongMemEval raw structure."""
import json

d = json.load(open(r"E:\eimemory\data\longmemeval_s_cleaned.json", encoding="utf-8"))
sample = d[0]
print("=== Top-level keys ===")
print(list(sample.keys()))

hs = sample.get("haystack_sessions") or []
print(f"\nhaystack_sessions: list len={len(hs)}")
if hs:
    s0 = hs[0]
    print(f"first session type={type(s0).__name__}")
    if isinstance(s0, dict):
        print(f"  keys: {list(s0.keys())}")
        for k, v in s0.items():
            preview = str(v)[:200]
            print(f"  {k}: {preview}")
    elif isinstance(s0, str):
        print(f"  raw string: {s0!r}")
    else:
        print(f"  raw: {s0!r}"[:300])

print(f"\nhaystack_session_ids: {sample.get('haystack_session_ids')}")
print(f"answer_session_ids: {sample.get('answer_session_ids')}")
print(f"answer: {sample.get('answer')}")
print(f"question: {sample.get('question')}")
print(f"question_type: {sample.get('question_type')}")
