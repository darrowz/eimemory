"""Attach exact independent-review provenance without double-counting functions."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--coverage', type=Path, required=True)
parser.add_argument('--review-dir', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
coverage = json.loads(args.coverage.read_text())
by_identity = {}
for path in sorted(args.review_dir.glob('*coverage.json')):
    peer = json.loads(path.read_text())
    base = peer.get('base_commit', peer.get('baseline_commit'))
    if base != coverage['baseline_commit']:
        continue
    for file in peer.get('files', []):
        for function in file.get('functions', []):
            start = function.get('start', function.get('line'))
            end = function.get('end', function.get('end_line'))
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            key = (file['path'], start, end)
            by_identity.setdefault(key, []).append({
                'artifact': 'review/' + path.name,
                'source_sha256': file['sha256'],
                'start': start,
                'end': end,
                'graph_node_id': function.get('graph_node_id'),
                'method': peer.get('method', ''),
            })
paired = []
first_pass = []
for file in coverage['files']:
    for function in file['functions']:
        key = (file['path'], function['start'], function['end'])
        if function['pass1'] != 'source_reviewed':
            continue
        first_pass.append(key)
        matches = [entry for entry in by_identity.get(key, [])
                   if entry['source_sha256'] == file['sha256']]
        if matches:
            function['pass2'] = 'independently_source_reviewed'
            function['pass2_evidence'] = matches
            paired.append(key)
        elif function['pass2'] == 'independently_source_reviewed':
            raise ValueError(f'paired function lacks matching independent source evidence: {key}')
assert len(first_pass) == len(set(first_pass))
assert len(paired) == len(set(paired))
coverage['reviewed_pass1_function_count'] = len(first_pass)
coverage['reviewed_pass2_function_count'] = len(paired)
for batch in coverage['batches']:
    definitions = [f for file in coverage['files'] for f in file['functions']
                   if f['batch'] == batch['id']]
    batch['pass1'] = sum(f['pass1'] == 'source_reviewed' for f in definitions)
    batch['pass2'] = sum(f['pass2'] == 'independently_source_reviewed' for f in definitions)
coverage['independent_review_artifacts'] = sorted({entry['artifact']
    for file in coverage['files'] for function in file['functions']
    for entry in function.get('pass2_evidence', [])})
coverage.pop('independent_review_artifact', None)
args.output.write_text(json.dumps(coverage, ensure_ascii=False, separators=(',', ':')) + '\n')
print(json.dumps({'unique_first_pass': len(first_pass), 'unique_paired': len(paired),
                  'paired_batch_counts': {b['id']: b['pass2'] for b in coverage['batches']}}, indent=2))
