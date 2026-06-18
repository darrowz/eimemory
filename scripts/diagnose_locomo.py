"""Diagnose LoCoMo 0.0 R@5: look at how cases are normalized, what chunks
look like, what the actual retrieval path is. Run on remote.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("EIMEMORY_ROOT", "/opt/eimemory"))
sys.path.insert(0, str(ROOT))
import json
import tempfile

# 1) Load a sample LoCoMo case and inspect its normalized form
DATA = ROOT / "data"
locomo = json.loads((DATA / "locomo10_eimemory.json").read_text(encoding="utf-8"))
case0 = locomo['cases'][0]
print('=== RAW LoCoMo case 0 ===')
print('case_id:', case0.get('case_id'))
print('question:', case0.get('question'))
print('question_type:', case0.get('question_type'))
print('expected_answer:', case0.get('expected_answer'))
print('evidence_session_ids:', case0.get('evidence_session_ids'))
print('evidence_turn_ids:', case0.get('evidence_turn_ids'))
print('n_haystack_sessions:', len(case0.get('haystack_sessions') or []))
if case0.get('haystack_sessions'):
    s0 = case0['haystack_sessions'][0]
    print('session[0] session_id:', s0.get('session_id'))
    print('session[0] n_turns:', len(s0.get('turns') or []))
    if s0.get('turns'):
        t0 = s0['turns'][0]
        print('turn[0] keys:', list(t0.keys()))
        print('turn[0] sample:', json.dumps(t0, ensure_ascii=False)[:300])

# 2) Run it through the LoCoMo adapter normalization and check chunks
print()
print('=== NORMALIZED LoCoMo case 0 ===')
from eimemory.evaluation.locomo import normalize_locomo_dataset, _returned_ids, _expected_ids
from eimemory.api.runtime import Runtime
norm = normalize_locomo_dataset({'name': 'locomo10-full', 'scope': locomo['scope'], 'cases': [case0]})
nc = norm['cases'][0]
print('normalized case_id:', nc['case_id'])
print('chunks count:', len(nc.get('chunks') or []))
if nc.get('chunks'):
    c0 = nc['chunks'][0]
    print('chunk[0] keys:', list(c0.keys()))
    print('chunk[0]:', json.dumps(c0, ensure_ascii=False)[:400])
print('evidence_session_ids:', nc.get('evidence_session_ids'))
print('evidence_turn_ids:', nc.get('evidence_turn_ids'))
print('evidence_chunk_ids:', nc.get('evidence_chunk_ids'))
exp_turn = _expected_ids(nc, granularity='turn')
print('_expected_ids(turn):', exp_turn)
print('len expected:', len(exp_turn))

# 3) Ingest + retrieve via Runtime
print()
print('=== INGEST + RETRIEVE ===')
tmp = Path(tempfile.mkdtemp(prefix='eim-diag-'))
runtime = Runtime.create(root=tmp)
try:
    from eimemory.evaluation.longmemeval import _ingest_case_chunks, _retrieve
    from eimemory.models.records import ScopeRef
    cs = ScopeRef.from_dict(nc.get('scope') or norm['scope'])
    _ingest_case_chunks(runtime, case=nc, scope=cs)
    # How many chunks ingested?
    raw_chunks = runtime.store.list_records(kinds=['raw_chunk'], scope=cs, limit=1000)
    print('raw_chunks in store:', len(raw_chunks))
    if raw_chunks:
        print('sample raw_chunk content keys:', list(raw_chunks[0].content.keys())[:8])
        print('sample raw_chunk text[:200]:', str(raw_chunks[0].content.get('text') or raw_chunks[0].content.get('raw_text') or '')[:200])
        print('sample raw_chunk turn_id:', raw_chunks[0].content.get('turn_id'))
        print('sample raw_chunk session_id:', raw_chunks[0].content.get('session_id'))

    # Try retrieve
    t0 = time.time()
    recs = _retrieve(runtime, query=nc['question'], scope=cs, mode='raw', limit=10)
    dt = (time.time() - t0) * 1000
    print(f'retrieve returned {len(recs)} records in {dt:.2f}ms')
    if recs:
        ret_turns = _returned_ids(recs, granularity='turn')
        print('returned turn_ids[:5]:', ret_turns[:5])
        print('overlap with expected:', len(set(ret_turns) & exp_turn))

    # Direct store search as fallback path
    t0 = time.time()
    recs2 = runtime.store.search(query=nc['question'], kinds=['raw_chunk'], scope=cs, limit=10)
    dt = (time.time() - t0) * 1000
    print(f'store.search returned {len(recs2)} records in {dt:.2f}ms')

    # How many chunks in store have non-empty text?
    nonempty = sum(1 for c in raw_chunks if (c.content.get('text') or c.content.get('raw_text') or '').strip())
    print(f'raw_chunks with non-empty text: {nonempty}/{len(raw_chunks)}')
    # Sample non-empty text length distribution
    lengths = [len(c.content.get('text') or c.content.get('raw_text') or '') for c in raw_chunks]
    if lengths:
        print(f'text lengths: min={min(lengths)} max={max(lengths)} avg={sum(lengths)//len(lengths)}')
finally:
    runtime.close()

