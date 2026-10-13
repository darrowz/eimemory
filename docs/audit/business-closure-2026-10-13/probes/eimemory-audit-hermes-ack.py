import sys, tempfile, json
sys.path.insert(0, '/workspace/eimemory')
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.retrieval.proactive import ProactiveRecallService
scope={'tenant_id':'default','agent_id':'audit','workspace_id':'audit','user_id':'auditor'}
release={'release_commit':'a'*40,'release_version':'audit','deployment_receipt_id':'audit','release_session_id':'audit'}
with tempfile.TemporaryDirectory() as directory:
 with Runtime.create(root=directory) as runtime:
  runtime.proactive=ProactiveRecallService(runtime,control_percent=0,release_identity=release)
  bridge=EIBrainRPCBridge(runtime)
  class Client:
   def __init__(self): self.calls=[]; self.fail_ack=True
   def call_or_bypass(self,method,params):
    self.calls.append((method,params))
    if method=='adapter.proactive_ack' and self.fail_ack:
     self.fail_ack=False
     return {'ok':False,'bypassed':True,'error':'adapter_timeout','result':None}
    return dict(bridge.handle({'method':method,'params':params}))
  client=Client(); provider=HermesMemoryProviderCore(client=client)
  provider.initialize('audit-session',agent_identity='audit',agent_workspace='audit',user_id='auditor')
  record=runtime.memory.ingest(text='Project Borealis requires primary-source citations.',title='Borealis requirements',memory_type='preference',scope=resolve_channel_scope('hermes',scope),source='hermes.memory',source_id='hermes')
  query='What does project Borealis require?'
  context=provider.prefetch(query,session_id='audit-session')
  row=runtime.store.sqlite.conn.execute('SELECT decision_id,citation,state,ever_injected FROM proactive_decision_items').fetchone()
  assert row is not None,context
  citation=row['citation']; decision=row['decision_id']
  provider.on_pre_llm_call(user_message=query,session_id='audit-session',turn_id='host-one')
  provider.on_post_llm_call(user_message=query,assistant_message='I used ['+citation+'].',session_id='audit-session',turn_id='host-one',injected_citations=[citation])
  before=runtime.store.load_proactive_decision(decision)
  provider.on_pre_llm_call(user_message='Followup',session_id='audit-session',turn_id='host-two')
  after=runtime.store.load_proactive_decision(decision)
  print(json.dumps({'context_delivered':citation in context,'terminal':after['terminal'],'item_state':after['items'][0]['state'],'ever_injected':after['items'][0]['ever_injected'],'ack_call_count':sum(m=='adapter.proactive_ack' for m,p in client.calls),'pending_terminal_retries':provider.pending_terminal_retry_count,'terminal_used_citations':[p['used_citations'] for m,p in client.calls if m=='adapter.proactive_terminal']},sort_keys=True))
  provider.shutdown()
