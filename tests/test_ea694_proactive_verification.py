"""Selected live functions and inert dependencies; no project imports."""
from pathlib import Path
import re, textwrap, types, unittest, json
SOURCE=Path(__file__).resolve().parents[1] / 'eimemory/adapters/openclaw/hooks.py'
def extract(name):
    lines=SOURCE.read_text().splitlines(True)
    start=next(i for i,l in enumerate(lines) if re.match(r'\s*def '+re.escape(name)+r'\(',l))
    indent=len(lines[start])-len(lines[start].lstrip())
    end=start+1
    while end<len(lines):
        line=lines[end]
        if line.strip() and len(line)-len(line.lstrip())<=indent and (line.lstrip().startswith("def ") or line.lstrip().startswith("@") or line.lstrip().startswith("class ")):break
        end+=1
    return textwrap.dedent(''.join(lines[start:end]))
def load(name,ns):
    text=extract(name)
    exec(compile('from __future__ import annotations\n'+text,str(SOURCE)+':'+name,'exec'),ns)
    return ns[name]
ns={'re':re,'Any':object}
load('_is_unexecuted_verification_state',ns)
class FakeLoop:
    def record_verification(self,*a,**k):self.check=k
    def finish_task(self,*a,**k):return k
loop=FakeLoop(); ns.update({'os':types.SimpleNamespace(environ={}), 'openclaw_loop':loop})
class Harness:pass
for name in ['_bool_or_none','_first_text','_first_present','_openclaw_loop_close']:
    setattr(Harness,name,load(name,ns))
Harness._classify_terminal_failure=lambda *a,**k:None
class VerificationContract(unittest.TestCase):
 def test_matrix(self):
    f=load('_proactive_terminal_outcome',ns)
    h=Harness()
    for ending in ['agent_end','task_end','session_end']:
     for success in [True,False]:
      for state in ['not run','not executed','skipped','unknown','page loaded']:
       with self.subTest(ending=ending,success=success,state=state):
        actual=f(h,event={},outcome={'success':success},verification=state,end_kind=ending)
        expected={} if ending=='session_end' or state!='page loaded' else {'verified':True,'success':success}
        self.assertEqual(actual,expected)
 def test_existing_loop_contract(self):
    h=Harness()
    for ending in ['agent_end','task_end']:
     for success in [True,False]:
      for state in ['not run','not executed','skipped','unknown','page loaded']:
       with self.subTest(ending=ending,success=success,state=state):
        result=h._openclaw_loop_close(event={'loop_task_id':'fake'},task_context={},outcome={'success':success},result='',verification=state,end_kind=ending)
        self.assertEqual(loop.check['passed'],success and state=='page loaded')
        self.assertEqual(result['status'],'done' if loop.check['passed'] else 'failed')
 def test_absent_success_and_empty_verification(self):
    f=load('_proactive_terminal_outcome',ns);h=Harness()
    self.assertEqual(f(h,event={},outcome={},verification='page loaded',end_kind='agent_end'),{})
    self.assertEqual(f(h,event={},outcome={'success':True},verification='',end_kind='agent_end'),{})
 def test_failure_is_valid_verified_outcome(self):
    f=load('_proactive_terminal_outcome',ns);h=Harness()
    self.assertEqual(f(h,event={'success':False},outcome={},verification='page loaded',end_kind='task_end'),{'verified':True,'success':False})
if __name__=='__main__':
 import sys
 unittest.main(argv=[sys.argv[0]],verbosity=2)
