"""Join fresh source coverage to graph IDs; no project imports or execution."""
from pathlib import Path
import argparse
import hashlib
import json

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, required=True, help='Checkout of the exact baseline source commit')
parser.add_argument('--graph-index', type=Path, required=True, help='Directory containing index.json and module-dependencies.json')
parser.add_argument('--audit-dir', type=Path, required=True, help='Directory containing coverage-manifest.json; outputs are written here')
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
issues = [
    {'id': 'A-STO-001', 'severity': 'P1', 'status': 'source_confirmed_twice',
     'nodes': ['n9446', 'n9447'], 'repair_status': 'independently_reviewed_and_published',
     'source_report': 'storage-ownership-batch-001.md',
     'remote_commit': 'd1d571e109c99eab6540fd41f8d167e2502f012a'},
    {'id': 'A-STO-002', 'severity': 'P1', 'status': 'source_confirmed_twice',
     'nodes': ['n9434', 'n9437', 'n9471', 'n9472'], 'repair_status': 'planned',
     'source_report': 'storage-ownership-batch-001.md', 'remote_commit': None},
    {'id': 'A-STO-003', 'severity': 'P1', 'status': 'source_confirmed_twice',
     'nodes': ['n9677', 'n9676', 'n9421'], 'repair_status': 'queued',
     'source_report': 'retrieval-collection-batch-002.md', 'remote_commit': None},
]
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
