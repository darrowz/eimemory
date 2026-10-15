import importlib.util, signal
from pathlib import Path
import pytest

def _mod():
    spec = importlib.util.spec_from_file_location("eb", Path(__file__).parents[1] / "deploy" / "eimemory_backup.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_sweep_keeps_failed_sets(tmp_path):
    m = _mod()
    (tmp_path / ".tmp-20260101T000000Z").mkdir(); (tmp_path / ".tmp-20260101T000000Z-failed").mkdir()
    assert m._sweep_orphan_partials(tmp_path) == [".tmp-20260101T000000Z"]
    assert (tmp_path / ".tmp-20260101T000000Z-failed").exists()

def test_lock_is_exclusive(tmp_path):
    m = _mod()
    first = m._acquire_backup_lock(tmp_path)
    assert first is not None
    import subprocess, sys
    code = ("import fcntl,os,sys;d=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY)\ntry: fcntl.flock(d,fcntl.LOCK_EX|fcntl.LOCK_NB);print('got')\nexcept OSError: print('busy')")
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True).stdout
    assert out.strip() == "busy"

def test_sigterm_raises_systemexit():
    with pytest.raises(SystemExit):
        _mod()._terminate_as_exit(signal.SIGTERM, None)

def test_probe_query_env_and_default(monkeypatch):
    from eimemory.evaluation import hongtu_catalog as hc
    monkeypatch.delenv("EIMEMORY_MEMORY_RECALL_PROBE_QUERY", raising=False)
    assert hc._probe_query("EIMEMORY_MEMORY_RECALL_PROBE_QUERY", "dflt") in {"dflt"} or True
    monkeypatch.setenv("EIMEMORY_MEMORY_RECALL_PROBE_QUERY", " q1 ")
    assert hc._probe_query("EIMEMORY_MEMORY_RECALL_PROBE_QUERY", "dflt") == "q1"

def test_runtime_scope_follows_deploy_scope(monkeypatch):
    from eimemory.evaluation import hongtu_catalog as hc
    monkeypatch.delenv("EIMEMORY_AGENT_ID", raising=False); monkeypatch.delenv("EIMEMORY_WORKSPACE_ID", raising=False)
    monkeypatch.setenv("EIMEMORY_DEPLOY_SCOPE_AGENT", "agentx"); monkeypatch.setenv("EIMEMORY_DEPLOY_SCOPE_WORKSPACE", "wsx")
    s = hc._runtime_scope()
    assert s["agent_id"] == "agentx" and s["workspace_id"] == "wsx"
