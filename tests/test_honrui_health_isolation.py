"""Health collection must work from a cold isolated interpreter, not only pytest."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from deploy import collect_release_health as collector
from deploy import verify_release_health as verifier

ROOT = Path(collector.__file__).resolve().parents[1]

@contextmanager
def server(raw: bytes, status=200, headers=None):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *_args):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}/health', seen
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=3)


def child_environment():
    env = dict(os.environ)
    env.update(EIMEMORY_RPC_AUTH_TOKEN='test-only-token', NO_PROXY='127.0.0.1,::1',
               no_proxy='127.0.0.1,::1')
    env.pop('PYTHONHOME', None)
    return env


def bytecode_snapshot():
    return {p.relative_to(ROOT):sha256(p.read_bytes()).hexdigest()
            for folder in (ROOT/'eimemory',ROOT/'deploy') for p in folder.rglob('*.pyc')}


def test_collector_cold_isolated_import_ignores_poisoned_cwd(tmp_path):
    poison = tmp_path / 'eimemory'; poison.mkdir()
    (poison/'__init__.py').write_text('raise RuntimeError("wrong package loaded")\n')
    before = bytecode_snapshot()
    env = child_environment(); env['PYTHONPATH'] = str(tmp_path)
    with server(b'{"ok":true,"service":"eimemory-rpc"}') as (url, seen):
        result = subprocess.run([sys.executable, '-I', '-S', '-B',
            str(ROOT/'deploy/collect_release_health.py'), '--probe-only', '--url', url],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['ok'] is True and payload['probe_only'] is True
    assert seen[0]['Authorization'] == 'Bearer test-only-token'
    assert before == bytecode_snapshot()


@pytest.mark.skipif(os.name != 'posix', reason='user-systemd owner contract requires POSIX')
def test_owner_check_uses_cold_collector_without_installed_package(tmp_path):
    binary = tmp_path/'bin'; binary.mkdir()
    tool = binary/'systemctl'
    tool.write_text('''#!/usr/bin/env bash
if [ "${1:-}" = "--user" ]; then
  shift
  case "${1:-}" in is-active) echo active;; is-enabled) echo enabled;; esac
else
  case "${1:-}" in is-active) echo inactive;; is-enabled) echo disabled;;
    show) case "${4:-}" in LoadState) echo not-found;; esac;; esac
fi
''')
    tool.chmod(0o755)
    env = child_environment(); env['PATH'] = str(binary)+os.pathsep+env.get('PATH','')
    env['EIMEMORY_HEALTH_PYTHON'] = sys.executable
    with server(b'{"ok":true,"service":"eimemory-rpc"}') as (url, _):
        env['LOOPBACK_HEALTH_URL'] = url
        result = subprocess.run(['bash', str(ROOT/'deploy/check_user_systemd_owner.sh')],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stderr+result.stdout
    assert 'ok=user_systemd_owner' in result.stdout
    env['EIMEMORY_HEALTH_COLLECTOR'] = str(tmp_path/'absent-collector.py')
    result = subprocess.run(['bash', str(ROOT/'deploy/check_user_systemd_owner.sh')],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=12)
    assert result.returncode != 0
    assert 'health_collector_unavailable' in result.stdout


@pytest.mark.parametrize('raw', [b'{"ok":true,"ok":false}', b'{"ok":true,"score":NaN}',
    b'{"ok":true,"score":1e309}', b'['*33+b'0'+b']'*33, b'null'])
def test_health_rejects_ambiguous_json(raw):
    with server(raw) as (url, _):
        assert verifier.fetch_health(url).get('_fetch_error')


@pytest.mark.parametrize('timeout', [True, 0, -1, float('nan'), float('inf'), 'not-number'])
def test_health_rejects_bad_timeout_before_network(timeout,monkeypatch):
    def forbidden():raise AssertionError('credentials/network reached before validation')
    monkeypatch.setattr(verifier,'rpc_probe_headers',forbidden)
    assert verifier.fetch_health('http://127.0.0.1:1/health', timeout=timeout).get('_fetch_error')


@pytest.mark.parametrize('url', ['http://user:secret@127.0.0.1/health',
    'http://127.0.0.1/health#fragment', 'http://127.0.0.1:0/health',
    'http://127.0.0.1:65536/health', 'https://127.0.0.1/health', 'http://example.org/'])
def test_health_rejects_untrusted_url_before_credentials(url,monkeypatch):
    def forbidden():raise AssertionError('credentials/network reached before validation')
    monkeypatch.setattr(verifier,'rpc_probe_headers',forbidden)
    assert verifier.fetch_health(url).get('_fetch_error')


def test_health_redirect_is_not_followed():
    with server(b'', 302, {'Location':'http://example.org/'}) as (url, seen):
        result = verifier.fetch_health(url)
    assert result.get('_fetch_error') and len(seen) == 1


def test_collector_missing_identity_does_not_downgrade_to_probe(monkeypatch):
    def forbidden(*a, **kw): raise AssertionError('network must not be called')
    monkeypatch.setattr(collector, '_load_verify_release_health', forbidden)
    result = collector.collect_release_health(url='http://127.0.0.1:1/health')
    assert result['ok'] is False


@pytest.mark.parametrize('paths', [None, [], 'not-an-object', 3])
def test_health_malformed_paths_is_rejected_not_crash(tmp_path, paths):
    (tmp_path/'eimemory').mkdir()
    result = verifier.verify_health_payload({'ok':True,'paths':paths},
        commit='a'*40, version='1.14.4', release_dir=tmp_path)
    assert result['ok'] is False
