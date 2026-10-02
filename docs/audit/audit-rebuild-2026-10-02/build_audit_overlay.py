"""Join fresh source coverage to graph IDs; no project imports or execution."""
from pathlib import Path
import argparse
import hashlib
import json

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, required=True, help='Checkout of the exact baseline source commit')
parser.add_argument('--graph-index', type=Path, required=True, help='Directory containing index.json and module-dependencies.json')
parser.add_argument('--audit-dir', type=Path, required=True, help='Directory containing coverage-manifest.json and finding-ledger.json; outputs are written here')
args = parser.parse_args()
SOURCE = args.source_root.resolve()
GRAPH = args.graph_index.resolve()
OUT = args.audit_dir.resolve()
graph = json.loads((GRAPH / 'index.json').read_text())
coverage_path = OUT / 'coverage-manifest.json'
coverage = json.loads(coverage_path.read_text())
assert graph['metadata']['commit'] == coverage['baseline_commit']
functions = {(n['file'], n['line'], n['end_line']): n for n in graph['nodes']
             if n['kind'] in {'function', 'method'} and 'line' in n}
files = {n['file']: n for n in graph['nodes'] if n['kind'] == 'file'}
by_name = {n.get('qualified_name'): n for n in graph['nodes'] if 'qualified_name' in n}
finding_ledger = json.loads((OUT / 'finding-ledger.json').read_text())
assert finding_ledger['baseline_commit'] == coverage['baseline_commit']
issues = finding_ledger['issues']
issue_by_node = {}
for issue in issues:
    for node in issue['nodes']:
        issue_by_node.setdefault(node, []).append(issue['id'])
node_states = []
for row in coverage['files']:
    assert hashlib.sha256((SOURCE / row['path']).read_bytes()).hexdigest() == row['sha256']
    row['graph_file_node_id'] = files[row['path']]['id']
    for function in row['functions']:
        key = (row['path'], function['start'], function['end'])
        node = functions[key]
        function['graph_node_id'] = node['id']
        function['findings'] = issue_by_node.get(node['id'], [])
        state = ('reviewed_twice' if function['pass2'] == 'independently_source_reviewed'
                 else 'reviewed_once' if function['pass1'] == 'source_reviewed' else 'unreviewed')
        node_states.append({'node_id': node['id'], 'path': row['path'], 'name': function['name'],
                            'state': state, 'findings': function['findings'], 'batch': function['batch']})
    reviewed = sum(f['pass1'] == 'source_reviewed' for f in row['functions'])
    twice = sum(f['pass2'] == 'independently_source_reviewed' for f in row['functions'])
    node_states.append({'node_id': row['graph_file_node_id'], 'path': row['path'],
                        'state': 'unreviewed' if not reviewed else 'fully_reviewed_twice' if twice == row['function_count'] else 'partially_reviewed',
                        'function_denominator': row['function_count'], 'reviewed_once_or_more': reviewed, 'reviewed_twice': twice})
# These are manually read call sites, distinct from the graph's static imports.
call_specs = [
    ('eimemory.retrieval.engine.GovernedRecallEngine._recall.search_scope_groups', 'eimemory.retrieval.sqlite_source.SQLiteCandidateSource.search', 724, 'candidate_source.search dynamic dispatch; SQLite destination conditional on source selection'),
    ('eimemory.retrieval.sqlite_source.SQLiteCandidateSource.search', 'eimemory.retrieval.sqlite_source.SQLiteCandidateSource._search', 126, 'self._search'),
    ('eimemory.retrieval.sqlite_source.SQLiteCandidateSource._search', 'eimemory.retrieval.sqlite_source.SQLiteCandidateSource._search_locked', 141, 'self._search_locked inside recall_read_scope'),
    ('eimemory.retrieval.sqlite_source.SQLiteCandidateSource._search_locked', 'eimemory.storage.runtime_store.RuntimeStore.search_with_diagnostics', 256, 'self.store.search_with_diagnostics'),
    ('eimemory.storage.runtime_store.RuntimeStore.search', 'eimemory.storage.runtime_store.RuntimeStore.borrow_reader', 1174, 'self.borrow_reader before deadline scope'),
    ('eimemory.storage.runtime_store.RuntimeStore.search_with_diagnostics', 'eimemory.storage.runtime_store.RuntimeStore.borrow_reader', 1203, 'self.borrow_reader before deadline scope'),
    ('eimemory.storage.runtime_store.RuntimeStore.borrow_reader', 'eimemory.storage.runtime_store.RuntimeStore._ensure_readers', 473, 'self._ensure_readers'),
    ('eimemory.storage.runtime_store.RuntimeStore.search', 'eimemory.storage.sqlite_store.SqliteRecordStore.search_with_diagnostics', 1177, 'slot.store.search_with_diagnostics'),
    ('eimemory.storage.runtime_store.RuntimeStore.search_with_diagnostics', 'eimemory.storage.sqlite_store.SqliteRecordStore.search_with_diagnostics', 1206, 'slot.store.search_with_diagnostics'),
    ('eimemory.storage.sqlite_store.SqliteRecordStore.search_with_diagnostics', 'eimemory.storage.sqlite_store.SqliteRecordStore._candidate_rows', 4408, 'self._candidate_rows'),
    ('eimemory.storage.sqlite_store.SqliteRecordStore._candidate_rows', 'eimemory.storage.sqlite_store.SqliteRecordStore._collect_fts_candidates', 4679, 'deadline-gated FTS collection'),
    ('eimemory.storage.sqlite_store.SqliteRecordStore._candidate_rows', 'eimemory.storage.sqlite_store.SqliteRecordStore._collect_anchor_candidates', 4690, 'deadline-gated anchor collection'),
    ('eimemory.storage.sqlite_store.SqliteRecordStore._candidate_rows', 'eimemory.storage.sqlite_store.SqliteRecordStore._collect_lane_seed_candidates', 4699, 'deadline-gated lane seed collection'),
    ('eimemory.storage.sqlite_store.SqliteRecordStore._candidate_rows', 'eimemory.storage.sqlite_store.SqliteRecordStore._collect_recent_candidates', 4707, 'deadline-gated recent collection'),
    ('eimemory.retrieval.engine.GovernedRecallEngine._fuse_and_pool_items', 'eimemory.retrieval.fusion.fuse_ranked_components', 1408, 'normalize fusion policy with empty components'),
    ('eimemory.retrieval.engine.GovernedRecallEngine._fuse_and_pool_items', 'eimemory.retrieval.fusion.fuse_ranked_components', 1479, 'fuse ranked components within each scope group'),
    ('eimemory.retrieval.engine.GovernedRecallEngine._fuse_and_pool_items', 'eimemory.retrieval.fusion.page_pool_key', 1558, 'page representative grouping'),
    ('eimemory.retrieval.engine.GovernedRecallEngine._fuse_and_pool_items', 'eimemory.retrieval.engine.GovernedRecallEngine._fusion_record_token', 1369, 'full-reference hash token map'),
    ('eimemory.retrieval.proactive.ProactiveRecallService.decide', 'eimemory.retrieval.query_identity.effective_query_digest', 361, 'source call-site context only; full decide method remains unreviewed'),
    ('eimemory.retrieval.query_identity.effective_query_digest', 'eimemory.retrieval.query_identity.query_text_digest', 12, 'established task_type plus separator plus query digest'),
    ('eimemory.retrieval.engine.GovernedRecallEngine._hydrate_records_batch', 'eimemory.storage.runtime_store.RuntimeStore.get_by_exact_refs', 2382, 'batch_fn dynamic dispatch when store exposes get_by_exact_refs'),
    ('eimemory.storage.runtime_store.RuntimeStore.get_by_exact_refs', 'eimemory.storage.sqlite_store.SqliteRecordStore.get_by_exact_refs', 1500, 'runtime lock owns batched exact lookup'),
    ('eimemory.storage.runtime_store.RuntimeStore.get_by_exact_ref', 'eimemory.storage.sqlite_store.SqliteRecordStore.get_by_exact_ref', 1511, 'runtime lock owns single exact lookup'),
    ('eimemory.retrieval.engine.GovernedRecallEngine._record_is_exact_and_active', 'eimemory.storage.runtime_store.RuntimeStore.get_by_exact_ref', 2400, 'active record checked through exact reference lookup'),
    ('eimemory.retrieval.engine.GovernedRecallEngine._resolve_visible_record', 'eimemory.storage.runtime_store.RuntimeStore.list_by_record_id_exact_scope', 2452, 'visible scope enumeration delegates exact-scope lookup'),
    ('eimemory.storage.runtime_store.RuntimeStore.list_by_record_ids_exact_scopes', 'eimemory.storage.sqlite_store.SqliteRecordStore.list_by_record_ids_exact_scopes', 1559, 'batched multi-scope lookup under writer lock'),
    ('eimemory.storage.runtime_store.RuntimeStore._safe_post_commit_projection', 'eimemory.storage.record_export.export_record_markdown', 397, 'best-effort projection after main commit'),
    ('eimemory.storage.record_export.export_record_markdown', 'eimemory.storage.atomic_file.atomic_write_bytes', 93, 'publish rendered projection bytes'),
    ('eimemory.storage.bounded_jsonl.append_bounded_jsonl', 'eimemory.storage.atomic_file.interprocess_lock', 38, 'sidecar lock for participating diagnostic writers'),
    ('eimemory.storage.bounded_jsonl.append_bounded_jsonl', 'eimemory.storage.atomic_file.open_regular_binary', 41, 'read bounded existing diagnostic tail'),
    ('eimemory.storage.bounded_jsonl.append_bounded_jsonl', 'eimemory.storage.atomic_file.atomic_write_bytes', 58, 'publish complete tail plus new opaque diagnostic entry'),
    ('eimemory.storage.atomic_file.locked_json_update', 'eimemory.storage.atomic_file.interprocess_lock', 286, 'owner-held read-modify-write'),
    ('eimemory.storage.atomic_file.locked_json_update', 'eimemory.storage.atomic_file.read_json_strict', 288, 'load current structured state'),
    ('eimemory.storage.atomic_file.locked_json_update', 'eimemory.storage.atomic_file.atomic_write_json', 298, 'publish validated update'),
    ('eimemory.storage.atomic_file.atomic_write_json', 'eimemory.storage.atomic_file.atomic_write_bytes', 271, 'validated bounded serialization delegates byte publication'),
    ('eimemory.storage.atomic_file.atomic_write_bytes', 'eimemory.storage.atomic_file._fsync_directory', 246, 'directory durability step after os.replace'),
    ('eimemory.retrieval.postgres_sync.SQLiteProjectionReader.page', 'eimemory.retrieval.postgres_sync.SQLiteProjectionReader._ensure_contract_locked', 98, 'cold projection page initializes metadata'),
    ('eimemory.retrieval.postgres_sync.SQLiteProjectionReader.snapshot_token', 'eimemory.retrieval.postgres_sync.SQLiteProjectionReader._ensure_contract_locked', 186, 'general-record revision lookup initializes metadata'),
    ('eimemory.retrieval.postgres_sync.PostgresVectorIndexSynchronizer.sync', 'eimemory.retrieval.postgres_sync.ProjectionReader.page', 285, 'reader protocol dynamic dispatch; live or snapshot implementation'),
    ('eimemory.retrieval.incremental_sync.SnapshotProjectionReader.__init__', 'eimemory.retrieval.postgres_sync.SQLiteProjectionReader.page', 44, 'copy bounded projection pages into derived snapshot'),
    ('eimemory.retrieval.incremental_sync.delta_snapshot', 'eimemory.retrieval.postgres_sync.SQLiteProjectionReader.page', 102, 'read bounded changed storage keys'),
    ('eimemory.retrieval.incremental_sync.maintain_memory_projection', 'eimemory.retrieval.incremental_sync.SnapshotProjectionReader.__init__', 134, 'select durable snapshot path'),
    ('eimemory.retrieval.incremental_sync.maintain_memory_projection', 'eimemory.retrieval.incremental_sync.delta_snapshot', 149, 'select incremental journal path'),
    ('eimemory.retrieval.memory_projection_authority.MemoryProjectionAuthority.revision', 'eimemory.storage.runtime_store.RuntimeStore.read_consistent', 63, 'read revision under owner snapshot'),
    ('eimemory.retrieval.memory_projection_authority.MemoryProjectionAuthority.head', 'eimemory.storage.runtime_store.RuntimeStore.read_consistent', 74, 'read projection head under owner snapshot'),
    ('eimemory.retrieval.vector_sync_worker.maintain_index', 'eimemory.retrieval.postgres_cli.handle_vector_index_command', 25, 'worker status dispatch; CLI function not fully audited'),
    ('eimemory.retrieval.vector_sync_worker.maintain_index', 'eimemory.retrieval.postgres_cli.handle_vector_index_command', 33, 'worker sync dispatch; CLI function not fully audited'),
    ('eimemory.retrieval.postgres_cli.handle_vector_index_command', 'eimemory.retrieval.incremental_sync.maintain_memory_projection', 193, 'memory-only branch call-site context; full CLI function remains unreviewed'),
    ('eimemory.retrieval.postgres_vector.OpenAICompatibleEmbeddingProvider.embed', 'eimemory.retrieval.postgres_vector._Circuit.allow', 233, 'in-process circuit admission before preflight'),
    ('eimemory.retrieval.postgres_vector.OpenAICompatibleEmbeddingProvider.embed', 'eimemory.retrieval.postgres_vector._Circuit.success', 285, 'normal completion updates circuit'),
    ('eimemory.retrieval.postgres_vector.OpenAICompatibleEmbeddingProvider.embed', 'eimemory.retrieval.postgres_vector._Circuit.cancel', 293, 'request-limited timeout path; ownership contract unresolved'),
    ('eimemory.retrieval.postgres_vector.OpenAICompatibleEmbeddingProvider.embed', 'eimemory.retrieval.postgres_vector._Circuit.failure', 296, 'ordinary failure accounting'),
    ('eimemory.retrieval.postgres_vector.PostgresCandidateRepository._connect', 'eimemory.retrieval.postgres_vector._ConnectionGate.acquire', 424, 'take current invocation connection permit'),
    ('eimemory.retrieval.postgres_vector.PostgresCandidateRepository._connect', 'eimemory.retrieval.postgres_vector._remaining_timeout', 424, 'pre-acquisition budget check'),
    ('eimemory.retrieval.postgres_vector.PostgresCandidateRepository._connect', 'eimemory.retrieval.postgres_vector._remaining_timeout', 437, 'post-acquisition pre-factory budget check'),
    ('eimemory.retrieval.postgres_vector.PostgresCandidateRepository._connect', 'eimemory.retrieval.postgres_vector._GatedConnection.__init__', 433, 'idle raw connection lease handoff'),
    ('eimemory.retrieval.postgres_vector.PostgresCandidateRepository._connect', 'eimemory.retrieval.postgres_vector._GatedConnection.__init__', 454, 'new raw connection lease handoff; factory not executed in audit'),
    ('eimemory.retrieval.postgres_vector.PostgresCandidateRepository._connect', 'eimemory.retrieval.postgres_vector._ConnectionGate.release', 456, 'ordinary exception cleanup'),
    ('eimemory.retrieval.postgres_vector.PostgresCandidateRepository.read_index_state', 'eimemory.retrieval.postgres_vector.PostgresCandidateRepository._connect', 474, 'state read acquires owned lease'),
    ('eimemory.retrieval.postgres_vector._GatedConnection.close', 'eimemory.retrieval.postgres_vector.PostgresCandidateRepository._release_idle', 1355, 'end lease through repository pool owner'),
    ('eimemory.retrieval.postgres_vector._GatedConnection.close', 'eimemory.retrieval.postgres_vector._ConnectionGate.release', 1359, 'finally returns lease permit'),
]
verified_calls = []
for source, target, line, label in call_specs:
    a, b = by_name[source], by_name[target]
    verified_calls.append({'source': a['id'], 'target': b['id'], 'file': a['file'], 'callsite_line': line,
                           'basis': 'manually inspected source call site; not runtime execution', 'label': label})
covered_modules = {n['id'] for n in graph['nodes'] if n['kind'] == 'module' and n.get('file') in {x['path'] for x in coverage['files']}}
imports = [x for x in json.loads((GRAPH / 'module-dependencies.json').read_text())['edges']
           if x['source'] in covered_modules and x['target'] in covered_modules]
overlay = {'baseline_commit': coverage['baseline_commit'], 'graph_version': graph['metadata']['schema_version'],
           'warning': 'Node states are audit bookkeeping. Static imports and inspected call sites do not prove runtime reachability, semantic correctness, or completed tests.',
           'scope': {'files': len(coverage['files']), 'functions': sum(r['function_count'] for r in coverage['files']),
                     'pass1': coverage['reviewed_pass1_function_count'], 'pass2': coverage['reviewed_pass2_function_count']},
           'nodes': node_states, 'static_module_import_edges': imports, 'source_verified_call_edges': verified_calls, 'issues': issues,
           'unreviewed_default': 'All graph nodes not listed in this overlay remain unreviewed by this source-audit ledger.'}
(OUT / 'graph-audit-overlay.json').write_text(json.dumps(overlay, ensure_ascii=False, separators=(',', ':')) + '\n')
coverage_path.write_text(json.dumps(coverage, ensure_ascii=False, separators=(',', ':')) + '\n')
print(json.dumps({'overlay_node_count': len(node_states), 'matched_functions': len(node_states) - len(coverage['files']),
                  'source_verified_call_edges': len(verified_calls), 'static_module_import_edges': len(imports)}, indent=2))
