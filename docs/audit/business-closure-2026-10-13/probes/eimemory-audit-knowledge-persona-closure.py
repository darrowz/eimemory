"""Read-only project audit probes; writes only fresh temp runtime storage."""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, '/workspace/eimemory')
from eimemory.api.runtime import Runtime
from eimemory.cli.main import _cmd_intake
from eimemory.persona.cli import handle_persona_command
from eimemory.persona.correction import correction_from_user_text
from eimemory.persona.store import PersonaStore
root = Path(tempfile.mkdtemp(prefix='eimemory-audit-knowledge-persona-'))
runtime = Runtime.create(root=root)
scope = {'agent_id': 'audit', 'workspace_id': 'audit', 'user_id': 'audit'}
article = root / 'article.txt'
article.write_text('Graph memory systems preserve independent source evidence and compare observations before changing a knowledge claim.', encoding='utf-8')
runtime.sources.add_source({'source_id': 'audit-local', 'source_kind': 'manual', 'uri': str(article), 'title': 'Audit local article'})
capture = io.StringIO()
with contextlib.redirect_stdout(capture):
    assert _cmd_intake(SimpleNamespace(intake_command='report', limit=None, source_kind=''), runtime, scope) == 0
preview = json.loads(capture.getvalue())
registry_advanced = bool(runtime.sources.list_sources()[0].last_scanned_at)
runtime.close()
runtime = Runtime.create(root=root)
actual = runtime.run_knowledge_intake(persist=True, scope=scope)
intake = {'preview_candidate_count': preview['candidate_count'], 'preview_written': preview['written_count'], 'registry_advanced': registry_advanced, 'after_reopen_scanned': actual['scanned_count'], 'after_reopen_skipped_not_due': actual['skipped_not_due_count'], 'after_reopen_written': actual['written_count']}
assert intake == {'preview_candidate_count': 1, 'preview_written': 0, 'registry_advanced': True, 'after_reopen_scanned': 0, 'after_reopen_skipped_not_due': 1, 'after_reopen_written': 0}
store = PersonaStore(runtime.store)
store.record_correction(correction_from_user_text('too verbose'), scope=scope, idempotency_key='single-correction')
values = [store.load_state().traits.verbosity]
for _ in range(3):
    with contextlib.redirect_stdout(io.StringIO()):
        rc = handle_persona_command(SimpleNamespace(persona_command='evolve', dry_run=False), runtime, scope)
    assert rc == 0
    runtime.close()
    runtime = Runtime.create(root=root)
    store = PersonaStore(runtime.store)
    values.append(store.load_state().traits.verbosity)
persona = {'correction_count': len(store.list_corrections(scope=scope)), 'verbosity_after_each_cli_evolve_and_reopen': values}
assert persona == {'correction_count': 1, 'verbosity_after_each_cli_evolve_and_reopen': [0.25, 0.1, 0.0, 0.0]}
runtime.close()
print(json.dumps({'intake': intake, 'persona': persona}, sort_keys=True))
