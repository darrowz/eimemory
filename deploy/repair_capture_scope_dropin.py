#!/usr/bin/env python3
"""Re-render existing exact capture JSON for systemd; never grant new scopes."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

KEY = 'EIMEMORY_CAPTURE_QUERY_SCOPES'
DROPIN = 'eimemory-rpc.service.d/95-release-scope-binding.conf'


def render_capture_dropin(payload: bytes) -> bytes:
    from eimemory.core.strict_json import loads
    from eimemory.evaluation.query_input_vault import parse_capture_scopes

    try:
        text = payload.decode('utf-8')
        if len(payload) > 65536 or '\0' in text or '\r' in text or not text.endswith('\n'):
            raise ValueError
        lines, found, section = [], 0, ''
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith('['):
                section = stripped
            if KEY not in line or stripped.startswith(('#', ';')):
                lines.append(line)
                continue
            if section != '[Service]' or not line.startswith('Environment='):
                raise ValueError
            rhs = line[len('Environment='):].rstrip('\n')
            if rhs.startswith('"'):
                # Only the canonical whole-assignment encoding is accepted.
                assignment = loads(rhs, max_bytes=65536)
                if not isinstance(assignment, str):
                    raise ValueError
                assignment = assignment.replace('%%', '%')
            else:
                # Repair only the observed legacy producer shape. The runtime
                # continues to require valid JSON; it never parses this syntax.
                assignment = rhs
            prefix = KEY + '='
            if not assignment.startswith(prefix):
                raise ValueError
            raw = assignment[len(prefix):]
            loads(raw, max_bytes=65536)  # Reject ambiguous duplicate JSON keys.
            parse_capture_scopes(raw)   # Same exact six-coordinate opt-in check.
            # systemd strips JSON's inner quotes unless the *whole assignment*
            # is quoted and C-escaped. Percent is a systemd specifier escape.
            lines.append('Environment=' + json.dumps((prefix + raw).replace('%', '%%'),
                ensure_ascii=False) + '\n')
            found += 1
        if found > 1:
            raise ValueError
        rendered = ''.join(lines).encode('utf-8')
        if len(rendered) > 65536:
            raise ValueError
        return rendered
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('capture_scope_dropin_invalid') from None


def repair_capture_dropin(root: Path, *, check_only: bool = False) -> dict:
    # Load the fixed reviewed sibling even under python -I. No PATH import or
    # live release/config is used as executable input.
    helper = Path(__file__).with_name('refresh_user_control_plane_commit.py')
    spec = importlib.util.spec_from_file_location('_capture_dropin_writer', helper)
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    target = root / DROPIN
    configured = writer.write_payload_refresh(target, render_capture_dropin,
        check_only=check_only, missing_ok=True)
    if not configured:
        # Do not create an allowlist or enable capture on an unconfigured host.
        return {'capture_scope_dropin': 'not_configured'}
    return {'capture_scope_dropin': 'validated' if check_only else 'rendered'}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--systemd-root', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        result = repair_capture_dropin(args.systemd_root, check_only=args.check_only)
    except (ValueError, RuntimeError, OSError):
        parser.exit(2, 'capture_scope_dropin_refresh_failed\n')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
