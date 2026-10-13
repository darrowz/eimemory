import sys,json,tempfile
sys.path.insert(0,'/workspace/eimemory')
from eimemory.api.runtime import Runtime
from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.retrieval.proactive import ProactiveRecallService
payload=json.load(open('/tmp/eimemory-audit-openclaw-normalized.json'))
with tempfile.TemporaryDirectory() as root:
 with Runtime.create(root=root) as runtime:
  runtime.proactive=ProactiveRecallService(runtime,control_percent=0,release_identity={'release_commit':'a'*40,'release_version':'audit','deployment_receipt_id':'audit','release_session_id':'audit'})
  hooks=OpenClawMemoryHooks(runtime)
  record=runtime.memory.ingest(text='Project Borealis requires primary-source citations.',title='Borealis requirements',memory_type='preference',scope=hooks._scope_from_event(payload['after']),source='openclaw.memory',source_id='openclaw')
  results={}
  for key,event in payload.items():
   result=hooks.before_prompt_build(event)
   found=[r['record_id'] for r in result['memory_bundle'].get('items',[])]
   results[key]={'scope_user':hooks._scope_from_event(event)['user_id'],'private_target_returned':record.record_id in found,'returned_count':len(found)}
  print(json.dumps(results,sort_keys=True))
