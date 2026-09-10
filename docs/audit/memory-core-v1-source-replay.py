import os,sys,json,tempfile
from hashlib import sha256
from pathlib import Path
for key in list(os.environ):
    if key.startswith("EIMEMORY_"): del os.environ[key]
sys.path[:0]=[os.getcwd(), os.getcwd()+"/tests"]
from dataclasses import asdict
from contextlib import closing
from eimemory.models.records import RecordEnvelope,ScopeRef,TimeRef
from eimemory.api.runtime import Runtime
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.retrieval.lightweight_admission import LightweightAdmission,LightweightConfig
from eimemory.retrieval.evidence_fragments import evidence_fragments
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text,candidate_record_projection_digest
from eimemory.knowledge.sediment import extract_l1_atoms,_split_turn,_reject_extract
from eimemory.knowledge.l1_pipeline import extract_l1_from_l0_record
from eimemory.knowledge.turn_context import persist_same_turn_context
from test_recall_budget_reserve import fragment_source
source=json.load(open("/home/darrow/tmp/eimemory-core-v1/scoped-original-evidence-redacted.json"))
support_export=json.load(open('/home/darrow/tmp/eimemory-core-v1/same-turn-project-evidence.json'))
report={"simulation":"Injected equal cosine 0.9 and local fragment SQL rows; real local SQLite authority validation. No production index/embeddings verified.","records":[],"queries":[]}
report['input_sha256'] = {name: sha256(Path('/home/darrow/tmp/eimemory-core-v1', name).read_bytes()).hexdigest()
    for name in ['scoped-original-evidence-redacted.json', 'same-turn-project-evidence.json']}
with tempfile.TemporaryDirectory(prefix="core-replay-") as root, closing(Runtime.create(root=Path(root))) as runtime:
    records=[]
    for r in source["records"]:
        item=RecordEnvelope.create(kind="memory",title=r["title"],summary=r["summary"],content={"text":r["content_text"],"memory_type":r["memory_type"]},scope=ScopeRef.from_dict(r["scope"]),source=r["source"],source_id=r["source_id"],meta={"memory_type":r["memory_type"],"source_event_id":r["source_event_id"],**r["l1_metadata"]})
        item.record_id=r["record_id"]
        item.time=TimeRef(**r["stored_time"])
        runtime.store.append(item); records.append(item)
        user,assistant=_split_turn(user_text="",assistant_text="",turn_text=r["content_text"])
        atoms=extract_l1_atoms(turn_text=r["content_text"],source_message_ids=[item.record_id])
        report["records"].append({"id":item.record_id,"source_event_id":r["source_event_id"],"time":asdict(item.time),"fixture_projection_digest":candidate_record_projection_digest(item,max_text_chars=16000),"original_hash_provenance_only":r["original_content_sha256"],"user_length":len(user),"user_guard_rejects":_reject_extract(user),"recorded_l1_metadata":r["l1_metadata"],"atoms": [asdict(a) for a in atoms]})
    deployment = next(i for i in records if i.record_id == support_export['source_record_id'])
    original_deployment = deployment.to_dict()
    # Source partition comes from the bound captured parent, never a query.
    binding = {**support_export, 'source_id': deployment.source_id}
    report['project_context_written'] = persist_same_turn_context(runtime.memory, deployment, binding)
    assert len(report['project_context_written']) == 1
    assert runtime.store.get_by_id(deployment.record_id, scope=deployment.scope).to_dict() == original_deployment
    contextual = runtime.store.get_by_id(report['project_context_written'][0]['record_id'], scope=deployment.scope)
    records.append(contextual)
    report['project_context'] = contextual.provenance['project_context']
    report['project_context_evidence'] = contextual.evidence
    report['project_fixture_projection_digest'] = candidate_record_projection_digest(contextual,max_text_chars=16000)
    report['support_records'] = [runtime.store.get_by_id(i, scope=deployment.scope).to_dict()
                                 for i in contextual.evidence[1:]]
    for row in report['support_records']:
        message = next(m for m in support_export['messages'] if m['id'] == row['provenance']['source_message_id'])
        assert row['content']['text'] == message['content']
        assert row['scope'] == support_export['scope'] and row['source_id'] == deployment.source_id
        row['fixture_projection_digest'] = candidate_record_projection_digest(
            runtime.store.get_by_id(row['record_id'], scope=deployment.scope), max_text_chars=16000)
    def choose(request,fragments):
        # Simulate lexical fragment ranking; do not use admission as an oracle.
        import re
        terms=re.findall(r"[a-zA-Z0-9_.-]+|部署|验收|任务|进展",request.query)
        return max(fragments,key=lambda f:sum(t.casefold() in f["text"].casefold() for t in terms))
    runtime.memory.recall_engine.candidate_source=fragment_source(runtime.store,select_fragment=choose)
    runtime.memory.recall_engine.relevance_admission=LightweightAdmission(LightweightConfig(enabled=True))
    for label,method,q,context in [("named_release","adapter.search_l0","eimemory 1.13.9 部署 验收",{}),("inherited_project","adapter.search_l0","最近任务进展",{"project":"eimemory"}),("original_inherited_project","adapter.prefetch","最近 已授权 任务 进展 未完成 验收 最新 状态",{"project":"eimemory"}),("release_without_unrecorded_project","adapter.search_l0","1.13.9 部署 验收",{}),("global_history","adapter.search_l0","全局最近任务进展",{})]:
        out=EIBrainRPCBridge(runtime).handle({"method":method,"params":{"channel":"hermes","scope":source["records"][0]["scope"],"query":q,"task_context":context,"limit":1}})
        bundle=out.get("result",{}).get("bundle",{})
        entry={"label":label,"query":q,"context":context,"ok":out.get("ok"),"status":bundle.get("retrieval_status"),"items":bundle.get("items",[])}
        if label in {'named_release', 'inherited_project', 'original_inherited_project'}:
            assert entry['status'] == 'evidence_found', entry
            assert [x['record_id'] for x in entry['items']] == [contextual.record_id], entry
            assert entry['items'][0]['supporting_record_ids'] == contextual.evidence
            assert '正式业务闭环仍未通过' in entry['items'][0]['evidence_excerpt']
            assert 'budget_exhausted' in entry['items'][0]['evidence_excerpt']
        for compact in entry["items"]:
            parent=next(i for i in records if i.record_id==compact["record_id"])
            fs=evidence_fragments(candidate_record_keyword_text(parent,max_text_chars=16000))
            f=next(f for f in fs if f["id"]==compact["evidence_fragment_id"])
            assert compact["evidence_excerpt"] in f["text"]
            assert compact["source_id"]==parent.source_id
            compact["citation_fragment_verified"]=True
        report["queries"].append(entry)
    phone=next(i for i in records if i.record_id=="mem_cdeb4e51ae6eac73b3ca0689603ddb82")
    from eimemory.retrieval.answer_requirements import supports_answer_requirements
    report["admission_matrix"] = []
    for item in records[:3]:
        fs = evidence_fragments(candidate_record_keyword_text(item,max_text_chars=16000))
        report["admission_matrix"].append({"id":item.record_id,"checks":{
            q: [f["id"] for f in fs if supports_answer_requirements(q, f["text"])]
            for q in ["eimemory 1.13.9 部署 验收", "项目 eimemory 最近任务进展", "1.13.9 部署 验收", "全局最近任务进展"]}})
    assert all(not fragments for row in report['admission_matrix'][:2] for fragments in row['checks'].values())
    report["phone_written"]=extract_l1_from_l0_record(runtime.memory,phone)
    for row in report["phone_written"]:
        atom=runtime.store.get_by_id(row["record_id"],scope=phone.scope)
        report["phone_persisted"]={"time":asdict(atom.time),"evidence":atom.evidence,"links":[asdict(x) for x in atom.links],"text":atom.summary}
    runtime.memory.recall_engine.candidate_source=fragment_source(runtime.store,select_fragment=choose)
    bundle=runtime.memory.recall(query="鸿哥现在用的手机是什么型号？",scope=source["records"][0]["scope"],task_context={"exact_scope_only":True},limit=1)
    report["phone_recall"]=bundle.to_compact_dict()
    assert [x.record_id for x in bundle.items] == [report["phone_written"][0]["record_id"]]
    compact=report["phone_recall"]["items"][0]
    fs=evidence_fragments(candidate_record_keyword_text(bundle.items[0],max_text_chars=16000))
    f=next(f for f in fs if f["id"]==compact["evidence_fragment_id"])
    assert compact["evidence_excerpt"] in f["text"]
    report["phone_excerpt_verified"]=True
Path("/tmp/memory-core-third-source-replay.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({"queries":[{"label":q["label"],"status":q["status"],"ids":[x["record_id"] for x in q["items"]]} for q in report["queries"]],"extraction":[{"id":r["id"],"length":r["user_length"],"guard":r["user_guard_rejects"],"atoms":len(r["atoms"])} for r in report["records"]]},indent=2))
