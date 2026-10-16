import importlib.util, subprocess
from pathlib import Path

def _m():
    spec = importlib.util.spec_from_file_location("bpr", Path(__file__).parents[1] / "deploy" / "bootstrap_production_recall.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_keeps_existing_env(monkeypatch):
    m = _m(); monkeypatch.setenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", "/x")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no lookup")))
    m._adopt_release_scope_bindings_env()
    import os; assert os.environ["EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE"] == "/x"

def test_adopts_only_binding_var_from_live_service(monkeypatch, tmp_path):
    m = _m(); monkeypatch.delenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", raising=False)
    monkeypatch.delenv("EIMEMORY_OTHER", raising=False)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "4242\n")
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"EIMEMORY_OTHER=1\0EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE=/b.json\0" if str(self) == "/proc/4242/environ" else real(self))
    m._adopt_release_scope_bindings_env()
    import os
    assert os.environ["EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE"] == "/b.json" and "EIMEMORY_OTHER" not in os.environ
    os.environ.pop("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE")

def test_no_service_is_noop(monkeypatch):
    m = _m(); monkeypatch.delenv("EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE", raising=False)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "0\n")
    m._adopt_release_scope_bindings_env()
    import os; assert "EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE" not in os.environ
