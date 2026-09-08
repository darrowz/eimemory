import json
import queue
import sys

import pytest

from eimemory.llm.gateway_pool import GatewayPoolClient, close_pool


@pytest.fixture(autouse=True)
def isolated_pool():
    close_pool()
    yield
    close_pool()


SCRIPT = '''import sys,json,os,time
for line in sys.stdin:
 p=json.loads(line)
 if p['user_prompt']=='slow': time.sleep(.5)
 print(json.dumps({'request_id':p['request_id'],'result':{'text':json.dumps({'pid':os.getpid(),'value':p['user_prompt']}),'provider_id':'test','model_id':'test'}}),flush=True)
'''


def test_pool_reuses_at_most_two_clients_without_reusing_answers():
    client = GatewayPoolClient([sys.executable,'-u','-c',SCRIPT],identity_key='one',timeout_seconds=2)
    client.prepare()
    results = [json.loads(client.complete(system_prompt='policy',user_prompt=str(i)).text) for i in range(4)]
    assert [r['value'] for r in results] == ['0','1','2','3']
    assert len({r['pid'] for r in results}) == 2
    assert results[0]['pid'] == results[2]['pid']


def test_timeout_discards_worker_and_never_consumes_late_output():
    client = GatewayPoolClient([sys.executable,'-u','-c',SCRIPT],identity_key='one',timeout_seconds=.05)
    client.prepare()
    with pytest.raises(queue.Empty):
        client.complete(system_prompt='policy',user_prompt='slow')
    client.timeout_seconds = 2
    result = json.loads(client.complete(system_prompt='policy',user_prompt='next').text)
    assert result['value'] == 'next'
    result = json.loads(client.complete(system_prompt='policy',user_prompt='last').text)
    assert result['value'] == 'last'


def test_configuration_change_closes_previous_pool():
    first = GatewayPoolClient([sys.executable,'-u','-c',SCRIPT],identity_key='one',timeout_seconds=2)
    first.prepare()
    pool = first._pool()
    second = GatewayPoolClient([sys.executable,'-u','-c',SCRIPT],identity_key='two',timeout_seconds=2)
    second.prepare()
    assert pool.closed and all(w.process.poll() is not None for w in pool.workers)
    assert json.loads(second.complete(system_prompt='policy',user_prompt='fresh').text)['value'] == 'fresh'
