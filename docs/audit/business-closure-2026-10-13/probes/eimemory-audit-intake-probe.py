import sys, os, json, tempfile, time
sys.path.insert(0, '/workspace/eimemory')
from types import SimpleNamespace
from unittest.mock import patch
from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef

results=[]
os.environ['EIMEMORY_L1_FORCE_QUEUE']='1'
os.environ.pop('PYTEST_CURRENT_TEST',None)
os.environ.pop('EIMEMORY_L1_KICK_THREAD',None)
class Model:
    text=''
    def complete(self, **kw):
        prompt=kw['user_prompt']
        if prompt.startswith('{'):
            body=json.loads(prompt)
            data=[{'record_id':'new-0','action':'update','target_ids':body['new_memories'][0]['related_ids'],'merged_content':self.text,'merged_type':'fact'}]
        else:
            data=[{'content':self.text,'type':'fact','priority':90}]
        return SimpleNamespace(text=json.dumps(data))
class Bridge:
    def __init__(self, service): self.service=service; self.results=[]; self.params=[]
    def call_or_bypass(self, method, params):
        if method=='adapter.sync_turn':
            result=self.service.sync_turn(**params)
            self.results.append(result); self.params.append(params)
            return {'ok':True,'bypassed':False,'result':result}
        return {'ok':True,'bypassed':False,'result':{}}
model=Model()
with tempfile.TemporaryDirectory(prefix='audit-intake-') as root:
    runtime=Runtime.create(root=root); service=AgentRuntimeMemoryService(runtime)
    bridge=Bridge(service); provider=HermesMemoryProviderCore(client=bridge)
    provider.initialize('synthetic-session',agent_identity='audit',agent_workspace='isolated',user_id='owner')
    try:
        states=['Orchid hourly supervision setting is enabled.','Orchid hourly supervision setting is cancelled.','Orchid hourly supervision setting is enabled.']
        drains=[]
        with patch('eimemory.llm.hermes_adapter.resolve_l1_llm_client',return_value=model):
            for index,text in enumerate(states):
                model.text=text
                time.sleep(1.05)
                provider.sync_turn(text,'Acknowledged.',messages=[{'id':2*index+1,'role':'user','content':text},{'id':2*index+2,'role':'assistant','content':'Acknowledged.'}])
                provider.shutdown()
                drains.append(service._l1_queue().drain_report(lambda job: service._extract_l1_inline(user_text=job['user_text'],assistant_text=job['assistant_text'],turn_text=job['turn_text'],episode_id=job['episode_id'],channel_id=job['channel_id'],channel_scope=job['channel_scope'],session_id=job['session_id'],turn_id=job['turn_id']),limit=1))
        scope=ScopeRef.from_dict(bridge.results[0]['record']['scope'])
        rows=runtime.store.list_records(kinds=['memory'],scope=scope,limit=100)
        l0=[r for r in rows if business_metadata(r.meta).get('memory_layer')=='l0']
        l1=[r for r in rows if business_metadata(r.meta).get('memory_layer')=='l1']
        result={'probe':'distinct_identical_host_turns','real_host_row_ids':[[1,2],[3,4],[5,6]],'turn_ids':[p['turn_id'] for p in bridge.params], 'captured_ids':[r['record']['record_id'] for r in bridge.results], 'idempotent':[r.get('idempotent') for r in bridge.results], 'l0_count':len(l0),'l1_count':len(l1),'active_l1_states':['enabled' if 'enabled' in r.summary else 'cancelled' for r in l1 if r.status=='active'], 'expected_latest_state':'enabled', 'queue_drains':drains, 'provider_dropped_writes':provider.dropped_write_count, 'l0_completion_statuses':[business_metadata(r.meta).get('l1_extraction_status') for r in l0], 'l1_statuses':[r.status for r in l1]}
        assert result['captured_ids'][0] == result['captured_ids'][2]
        assert result['idempotent'] == [False,False,True]
        assert result['active_l1_states'] == ['cancelled']
        assert all(d['processed']==1 and d['failed']==d['pending']==0 for d in drains)
        results.append(result)
        print(json.dumps(result))
    finally: runtime.close()

class FailureBridge(Bridge):
    def __init__(self, service): super().__init__(service); self.sync_attempts=0
    def call_or_bypass(self,method,params):
        if method=='adapter.sync_turn':
            self.sync_attempts+=1
            if self.sync_attempts==1:
                raise TimeoutError('synthetic transport unavailable')
        return super().call_or_bypass(method,params)
with tempfile.TemporaryDirectory(prefix='audit-intake-failure-') as root:
    runtime=Runtime.create(root=root); service=AgentRuntimeMemoryService(runtime)
    client=FailureBridge(service); provider=HermesMemoryProviderCore(client=client)
    provider.initialize('failure-session',agent_identity='audit',agent_workspace='isolated',user_id='owner')
    try:
        lost='Synthetic Falcon durable account belongs to Alice.'
        kept='Synthetic Orchid durable account belongs to Bob.'
        provider.sync_turn(lost,'Acknowledged.')
        provider.shutdown()
        provider.sync_turn(kept,'Acknowledged.')
        provider.shutdown()
        status=json.loads(provider.handle_tool_call('eimemory_status',{}))
        scope=ScopeRef.from_dict(client.results[0]['record']['scope'])
        rows=runtime.store.list_records(kinds=['memory'],scope=scope,limit=100)
        result={'probe':'failed_capture_transport','sync_attempts':client.sync_attempts,
                'successful_server_captures':len(client.results),'lost_turn_in_l0':any(lost in r.summary for r in rows),
                'later_turn_in_l0':any(kept in r.summary for r in rows),'remaining_write_queue':len(provider._write_queue),
                'status_dropped_writes':status['adapter_local']['dropped_writes'],
                'status_pending_terminal_retries':status['adapter_local']['pending_terminal_retries'],
                'status_background_workers':status['adapter_local']['background_workers']}
        assert result['sync_attempts']==2 and result['successful_server_captures']==1
        assert result['lost_turn_in_l0'] is False and result['later_turn_in_l0'] is True
        assert result['remaining_write_queue']==result['status_dropped_writes']==0
        results.append(result); print(json.dumps(result))
    finally: runtime.close()
with open('/tmp/eimemory-audit-intake-results.json','w') as f: json.dump(results,f,indent=2)
