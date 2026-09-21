#!/usr/bin/env python3
"""Generate a review-only, hash-bound diff from ACTUAL Luna bridge source.

Does not import/execute the bridge, call a provider, overwrite source, or deploy.
Unsupported structure fails closed; no guessed response/model identity handling.
"""
from __future__ import annotations
import argparse
import ast
import difflib
from hashlib import sha256
import json
from pathlib import Path
import re
import sys


class Unsupported(ValueError):
    pass


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return base + '.' + node.attr if base else ''
    return ''


def instrument(source):
    if not source.endswith('\n') or '\r' in source:
        raise Unsupported('requires_utf8_lf_source_with_final_newline')
    tree = ast.parse(source)
    if '_luna_trace' in source or 'luna_observability' in source:
        raise Unsupported('already_instrumented_or_reserved_name')
    if any(isinstance(n, (ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom))
           for n in ast.walk(tree)):
        raise Unsupported('only_synchronous_one_shot_bridge_supported')
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and n.module == 'agent.auxiliary_client']
    if len(imports) != 1 or len(imports[0].names) != 1 or imports[0].names[0].name != 'resolve_provider_client':
        raise Unsupported('expected_one_exact_auxiliary_import')
    imported = imports[0]
    resolver = imported.names[0].asname or 'resolve_provider_client'
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    setup = [n for n in calls if dotted(n.func) == resolver]
    api = [n for n in calls if dotted(n.func).endswith('.chat.completions.create')]
    if len(setup) != 1 or len(api) != 1:
        raise Unsupported('expected_one_setup_and_one_api_call')
    setup, api = setup[0], api[0]
    kw = {k.arg: k.value for k in setup.keywords}
    if (not setup.args or not isinstance(setup.args[0], ast.Constant)
            or setup.args[0].value != 'openai-codex'
            or not isinstance(kw.get('model'), ast.Constant)
            or kw['model'].value != 'gpt-5.6-luna'
            or any(k.arg is None for k in setup.keywords)):
        raise Unsupported('provider_model_contract_not_literal_or_changed')
    kw = {k.arg: k.value for k in api.keywords}
    if (not isinstance(kw.get('reasoning_effort'), ast.Constant)
            or kw['reasoning_effort'].value != 'low'
            or any(k.arg is None for k in api.keywords)):
        raise Unsupported('reasoning_contract_not_literal_or_changed')
    if 'stream' in kw and (not isinstance(kw['stream'], ast.Constant) or kw['stream'].value is not False):
        raise Unsupported('streaming_response_timing_unsupported')
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    def assignment(call):
        parent = parents.get(call)
        if not isinstance(parent, (ast.Assign, ast.AnnAssign)) or parent.value is not call:
            raise Unsupported('calls_must_be_direct_assignments')
        ancestor = parent
        while ancestor in parents:
            ancestor = parents[ancestor]
            if isinstance(ancestor, (ast.For, ast.While, ast.comprehension, ast.Lambda)):
                raise Unsupported('looped_or_nested_inference_unsupported')
        return parent
    setup_stmt, api_stmt = assignment(setup), assignment(api)
    # Validation region must continue in the same lexical block after API return.
    if parents[setup_stmt] is not parents[api_stmt] or setup_stmt.lineno >= api_stmt.lineno:
        raise Unsupported('setup_and_api_must_be_ordered_in_same_block')
    for node in calls:
        if dotted(node.func) in {'os._exit', 'exec', 'eval', 'runpy.run_path'}:
            raise Unsupported('dynamic_execution_or_hard_exit_unsupported')
    # Do not silently transform aliases that send protocol bytes around capture.
    if 'sys.stdout.buffer' in source or 'sys.__stdout__' in source or 'sys.__stderr__' in source:
        raise Unsupported('direct_protocol_stream_access_unsupported')
    lines = source.splitlines(keepends=True)
    nodes = [imported, setup_stmt, api_stmt]
    replacements = []
    for node, field in zip(nodes, ('bridge_import_ms', 'bridge_client_setup_ms', 'provider_response_ms')):
        start, end = node.lineno - 1, node.end_lineno
        prefix = lines[start][:node.col_offset]
        if prefix.strip() or '\t' in prefix:
            raise Unsupported('unsupported_indentation_or_inline_statement')
        # End-of-line comments are fine; another statement is not.
        if ';' in lines[end-1][node.end_col_offset:]:
            raise Unsupported('semicolon_statement_unsupported')
        original = ''.join(lines[start:end])
        wrapped = prefix + f"with _luna_trace.stage('{field}'):\n"
        wrapped += ''.join('    ' + line if line.strip() else line for line in original.splitlines(keepends=True))
        if not wrapped.endswith('\n'):
            wrapped += '\n'
        if field == 'provider_response_ms':
            wrapped += prefix + '_luna_trace.begin_response_validation()\n'
        replacements.append((start, end, wrapped))
    for (start, end, wrapped) in sorted(replacements, reverse=True):
        lines[start:end] = [wrapped]
    staged = ''.join(lines)
    tree2 = ast.parse(staged)
    # Keep module docstring and future imports at module start, with shebang/comments.
    header_end = 0
    for index, node in enumerate(tree2.body):
        if (index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)) or (
                isinstance(node, ast.ImportFrom) and node.module == '__future__'):
            header_end = node.end_lineno
        else:
            header_end = max(header_end, node.lineno - 1)
            break
    lines = staged.splitlines(keepends=True)
    prefix = ("from time import perf_counter_ns as _luna_clock\n"
              "_luna_started_ns = _luna_clock()\n"
              "from luna_observability import BridgeTrace as _LunaTrace\n"
              "_luna_trace = _LunaTrace(_luna_started_ns)\n"
              "with _luna_trace.session(active=__name__ == '__main__'):\n")
    output = ''.join(lines[:header_end]) + prefix
    output += ''.join('    ' + line if line.strip() else line for line in lines[header_end:])
    compile(output, '<instrumented-luna-bridge>', 'exec')
    # Production call expressions and their arguments must remain byte-for-byte AST equivalent.
    after_calls = [n for n in ast.walk(ast.parse(output)) if isinstance(n, ast.Call)]
    for before in (setup, api):
        matches = [n for n in after_calls if dotted(n.func) == dotted(before.func)]
        if len(matches) != 1 or ast.dump(before) != ast.dump(matches[0]):
            raise Unsupported('provider_call_changed')
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch('[0-9a-f]{64}', args.expected_sha256):
            raise Unsupported('invalid_expected_digest')
        raw = args.source.read_bytes()
        if len(raw) > 1_000_000 or sha256(raw).hexdigest() != args.expected_sha256:
            raise Unsupported('source_digest_or_size_mismatch')
        source = raw.decode('utf-8')
        candidate = instrument(source)
        helper = Path(__file__).with_name('luna_observability.py').read_text()
        patch = ''.join(difflib.unified_diff(source.splitlines(keepends=True),
                    candidate.splitlines(keepends=True), fromfile='a/luna_review_command.py',
                    tofile='b/luna_review_command.py'))
        patch += ''.join(difflib.unified_diff([], helper.splitlines(keepends=True),
                    fromfile='/dev/null', tofile='b/luna_observability.py'))
        args.output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        for name, text in {'luna_review_command.py': candidate,
                           'luna_observability.py': helper,
                           'luna_review_command.observability.patch': patch,
                           'manifest.json': json.dumps({
                               'source_sha256':sha256(raw).hexdigest(),
                               'candidate_sha256':sha256(candidate.encode()).hexdigest(),
                               'helper_sha256':sha256(helper.encode()).hexdigest(),
                               'source_executed':False, 'original_overwritten':False,
                               'live_validation':False,
                           }, indent=2)+'\n'}.items():
            path = args.output_dir / name
            with path.open('x', encoding='utf-8') as stream:
                stream.write(text)
            path.chmod(0o600)
        print('candidate_generated_for_review_no_source_execution')
        return 0
    except (OSError, UnicodeError, SyntaxError, Unsupported) as exc:
        code = str(exc) if isinstance(exc, Unsupported) else 'source_or_output_unavailable'
        print('patch_generation_refused:' + code, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
