import json, os, stat
from pathlib import Path
from types import SimpleNamespace
import pytest
from eimemory.scheduler import jobs

OVERFLOW = 65534

def _meta(uid, mode):
    return SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)

@pytest.fixture
def userns(monkeypatch):
    monkeypatch.setattr(jobs, "_overflow_uid", lambda: OVERFLOW)
    monkeypatch.setattr(jobs, "_in_user_namespace", lambda: True)

ROOT = Path("/var/lib/eimemory")

def test_sandbox_accepts_safe_system_ancestor(userns):
    assert jobs._sandbox_ancestor_owner_ok(Path("/var/lib"), _meta(OVERFLOW, 0o755), eimemory_root=ROOT)
    assert jobs._sandbox_ancestor_owner_ok(Path("/"), _meta(OVERFLOW, 0o755), eimemory_root=ROOT)

def test_sandbox_accepts_sticky_world_writable_ancestor(userns):
    assert jobs._sandbox_ancestor_owner_ok(Path("/var"), _meta(OVERFLOW, 0o1777), eimemory_root=ROOT)

def test_sandbox_rejects_attacker_writable_ancestor(userns):
    assert not jobs._sandbox_ancestor_owner_ok(Path("/var/lib"), _meta(OVERFLOW, 0o777), eimemory_root=ROOT)
    assert not jobs._sandbox_ancestor_owner_ok(Path("/var/lib"), _meta(OVERFLOW, 0o775), eimemory_root=ROOT)

def test_sandbox_never_relaxes_root_or_below(userns):
    assert not jobs._sandbox_ancestor_owner_ok(ROOT, _meta(OVERFLOW, 0o700), eimemory_root=ROOT)
    assert not jobs._sandbox_ancestor_owner_ok(ROOT / "evaluation", _meta(OVERFLOW, 0o700), eimemory_root=ROOT)
    assert not jobs._sandbox_ancestor_owner_ok(Path("/srv"), _meta(OVERFLOW, 0o755), eimemory_root=ROOT)

def test_rejects_other_uid_and_outside_userns(monkeypatch):
    monkeypatch.setattr(jobs, "_overflow_uid", lambda: OVERFLOW)
    monkeypatch.setattr(jobs, "_in_user_namespace", lambda: True)
    assert not jobs._sandbox_ancestor_owner_ok(Path("/var"), _meta(1234, 0o755), eimemory_root=ROOT)
    monkeypatch.setattr(jobs, "_in_user_namespace", lambda: False)
    assert not jobs._sandbox_ancestor_owner_ok(Path("/var"), _meta(OVERFLOW, 0o755), eimemory_root=ROOT)
    assert not jobs._sandbox_ancestor_owner_ok(Path("/var"), _meta(OVERFLOW, 0o755), eimemory_root=None)

def test_normal_chain_still_loads_and_rejects_writable(tmp_path, monkeypatch):
    root = tmp_path / "root"; d = root / "evaluation"; d.mkdir(parents=True)
    for p in (tmp_path, root, d): os.chmod(p, 0o700)
    f = d / "ds.json"; f.write_text(json.dumps({"cases": []})); os.chmod(f, 0o600)
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    # tmp_path ancestors may be world-writable sticky /tmp: allowed by the existing rule
    jobs.load_json_dataset_with_evidence(str(f))
    os.chmod(d, 0o770)
    with pytest.raises(jobs.DatasetUnreadableError):
        jobs.load_json_dataset_with_evidence(str(f))

def test_probe_scope_configured_then_discovered(tmp_path, monkeypatch):
    from eimemory.evaluation import hongtu_catalog as hc
    monkeypatch.setattr(hc, "_deployment_scope", lambda: {"tenant_id": "default", "agent_id": "a1", "workspace_id": "w", "user_id": "u"})
    monkeypatch.setenv("EIMEMORY_MEMORY_RECALL_PROBE_SCOPE", json.dumps({"workspace_id": "w::channel::x", "user_id": "u2"}))
    assert hc._runtime_scope() == {"tenant_id": "default", "agent_id": "a1", "workspace_id": "w::channel::x", "user_id": "u2"}
    monkeypatch.delenv("EIMEMORY_MEMORY_RECALL_PROBE_SCOPE")
    monkeypatch.setattr(hc, "_configured_probe_scope", lambda base: None)
    base = tmp_path / "evaluation" / "agents" / "a1"; (base / "d").mkdir(parents=True)
    (base / "d" / "x.json").write_text(json.dumps({"scope": {"tenant_id": "default", "agent_id": "a1", "workspace_id": "w::channel::h", "user_id": "u3"}}))
    (base / "production_recall.current.json").write_text(json.dumps({"relative_path": "d/x.json"}))
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    assert hc._runtime_scope()["workspace_id"] == "w::channel::h"
